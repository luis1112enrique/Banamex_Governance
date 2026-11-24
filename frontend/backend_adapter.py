"""Demo and explicitly authorized test access to the existing backend.

Test credentials and source attestations live in an operator-owned JSON file,
never in profile selectors. Production, real Client and Auditor access stay closed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tomllib
from threading import RLock
import time
import unicodedata
from uuid import UUID
import warnings


_LOCK = RLock()
_SESSIONS: dict[str, dict] = {}
_THREAD_OWNERS: dict[str, str] = {}
_LOGIN_FAILURES: dict[str, list[float]] = {}
_SESSION_TTL = 3600
_ALLOWED_PROFILES = {"data_steward", "auditor", "cliente"}


@dataclass(frozen=True)
class AuthResult:
    access_token: str | None = None
    identity: str | None = None
    profile: str | None = None
    error: str | None = None


def _test_config() -> tuple[dict, str]:
    """Fail closed unless the operator explicitly authorizes the complete test corpus."""
    location = os.environ.get("BANAMEX_TEST_ACCESS_CONFIG", "")
    path = (
        Path(location).expanduser() if location
        else Path(__file__).resolve().parents[1] / ".streamlit" / "test_access.json"
    )
    if not path.is_absolute() or not path.is_file() or path.stat().st_size > 100_000:
        raise ValueError("La configuración de pruebas del servidor no es válida.")
    metadata = path.stat()
    if path.is_symlink() or path.parent.is_symlink() or metadata.st_uid != os.getuid():
        raise ValueError("La configuración de acceso debe pertenecer al usuario del servidor.")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("La configuración de acceso requiere permisos 0600.")
    import streamlit as st

    if st.get_option("server.address") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("El entorno de pruebas debe escuchar solo en loopback.")
    content = path.read_bytes()
    config = json.loads(content)
    if config.get("environment") != "test" or config.get("enabled") is not True:
        raise ValueError("Solo se permite un entorno de pruebas autorizado explícitamente.")
    sources = config.get("sources", {})
    required = ("postgres_host", "postgres_port", "postgres_database", "pinecone_index")
    if not all(isinstance(sources.get(key), str) and sources[key].strip() for key in required):
        raise ValueError("Falta fijar las fuentes de prueba autorizadas en el servidor.")
    if sources.get("entire_corpus_authorized") is not True:
        raise ValueError("Debe autorizarse explícitamente el corpus documental completo de pruebas.")
    users = config.get("users")
    if not isinstance(users, dict) or not users:
        raise ValueError("No hay identidades de prueba configuradas en el servidor.")
    if any(
        not isinstance(name, str) or not isinstance(user, dict)
        or user.get("profile") not in _ALLOWED_PROFILES
        or not isinstance(user.get("password_hash"), str)
        for name, user in users.items()
    ):
        raise ValueError("Las identidades del servidor no tienen un formato válido.")
    return config, hashlib.sha256(content).hexdigest()


def _local_test_config() -> tuple[dict, str]:
    """Accept a local developer principal only from an owner-only server file."""
    path = Path(__file__).resolve().parents[1] / ".streamlit" / "secrets.toml"
    if not path.is_file():
        raise ValueError("Configuración local ausente: falta .streamlit/secrets.toml.")
    metadata = path.stat()
    if path.is_symlink() or path.parent.is_symlink() or metadata.st_uid != os.getuid():
        raise ValueError("Configuración local rechazada: el archivo debe pertenecer al usuario del servidor.")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Configuración local rechazada: secrets.toml requiere permisos 0600.")
    import streamlit as st

    if st.get_option("server.address") not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Desarrollo local bloqueado: el servidor debe escuchar solo en loopback.")
    content = path.read_bytes()
    if len(content) > 100_000:
        raise ValueError("Configuración local demasiado grande.")
    config = tomllib.loads(content.decode()).get("banamex_local_test", {})
    if config.get("enabled") is not True or config.get("authorized_test_sources") is not True:
        raise ValueError("Desarrollo local deshabilitado o fuentes de prueba sin autorización explícita.")
    if config.get("profile") != "data_steward" or not isinstance(config.get("identity"), str):
        raise ValueError("La identidad local debe ser un Data Steward definido por el servidor.")
    if not config["identity"].strip() or len(config["identity"]) > 128:
        raise ValueError("La identidad local del servidor no es válida.")
    sources = config.get("sources", {})
    required = ("postgres_host", "postgres_port", "postgres_database", "pinecone_index")
    if not all(isinstance(sources.get(key), str) and sources[key].strip() for key in required):
        raise ValueError("Faltan los pines de fuentes de prueba en la configuración local.")
    return config, hashlib.sha256(content).hexdigest()


def backend_status() -> dict:
    """Report configuration facts, never infer the health of external services."""
    try:
        _test_config()
    except (OSError, ValueError, TypeError, AttributeError):
        return {
            "configured": False, "local_available": False, "category": "configuration",
            "message": "Falta una configuración segura de usuarios de prueba para iniciar sesión.",
        }
    return {
        "configured": True, "local_available": False, "category": "authorization",
        "message": "Usuarios de prueba configurados. Inicia sesión; disponibilidad de servicios no comprobada.",
    }


def get_local_access(portal: str) -> AuthResult:
    """Issue a server-derived test principal without a fake login screen."""
    try:
        config, fingerprint = _local_test_config()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return AuthResult(error=str(exc))
    if portal != "institucional":
        return AuthResult(error="Cliente real pendiente: el backend necesita aislamiento de datos por cliente.")
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _SESSIONS[token] = {
            "identity": config["identity"], "profile": "data_steward", "portal": "institucional",
            "expires": time.monotonic() + _SESSION_TTL,
            "config_fingerprint": fingerprint, "kind": "local",
        }
    return AuthResult(token, config["identity"], "data_steward")


def _session_config(session: dict) -> tuple[dict, str]:
    return _local_test_config() if session.get("kind") == "local" else _test_config()


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$")
        iterations = int(rounds)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if algorithm != "pbkdf2_sha256" or not 300_000 <= iterations <= 2_000_000:
            return False
        if not 16 <= len(salt) <= 64 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def authenticate_test_user(username: str, password: str, *, portal: str) -> AuthResult:
    """Verify externally configured credentials; derive profile on the server."""
    if (
        not isinstance(username, str) or not isinstance(password, str)
        or len(username) > 128 or len(password) > 1024
    ):
        return AuthResult(error="Credenciales de prueba no válidas.")
    username = username.strip()
    try:
        config, fingerprint = _test_config()
    except (OSError, ValueError, TypeError, AttributeError):
        return AuthResult(error="El servidor no tiene configurado un entorno de pruebas autorizado.")
    now = time.monotonic()
    with _LOCK:
        failures = [stamp for stamp in _LOGIN_FAILURES.get(username, []) if now - stamp < 60]
        _LOGIN_FAILURES[username] = failures
        if len(failures) >= 5:
            return AuthResult(error="Demasiados intentos. Espera un minuto antes de continuar.")
    user = config["users"].get(username, {})
    profile = user.get("profile")
    valid_password = _password_matches(password, user.get("password_hash", ""))
    allowed_portal = "cliente" if profile == "cliente" else "institucional"
    if not valid_password or profile not in _ALLOWED_PROFILES or portal != allowed_portal:
        with _LOCK:
            _LOGIN_FAILURES[username].append(now)
        return AuthResult(error="Credenciales o portal no autorizados.")
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _LOGIN_FAILURES.pop(username, None)
        _SESSIONS[token] = {
            "identity": username, "profile": profile, "portal": portal,
            "expires": now + _SESSION_TTL, "config_fingerprint": fingerprint,
            "authentication": "password",
        }
    return AuthResult(token, username, profile)


def get_access_context(access_token: str | None) -> dict | None:
    if not isinstance(access_token, str):
        return None
    with _LOCK:
        session = _SESSIONS.get(access_token)
        if not session:
            return None
        try:
            _, fingerprint = _session_config(session)
        except (OSError, ValueError, TypeError, AttributeError):
            return None
        if session["expires"] < time.monotonic() or session["config_fingerprint"] != fingerprint:
            _SESSIONS.pop(access_token, None)
            return None
        return {key: session[key] for key in ("identity", "profile", "portal")}


def revoke_access(access_token: str | None) -> None:
    with _LOCK:
        _SESSIONS.pop(access_token, None)


def is_authenticated_login(access_token: str | None) -> bool:
    """A local automatic principal is never a password-authenticated login."""
    if get_access_context(access_token) is None:
        return False
    with _LOCK:
        session = _SESSIONS.get(access_token, {})
        return session.get("authentication") == "password" and session.get("kind") != "local"



@dataclass(frozen=True)
class AssistantResponse:
    final_answer: str
    chart_path: str | None = None
    error: str | None = None
    error_code: str | None = None


def validate_chart_path(value: object) -> str | None:
    """Accept only an existing, bounded local PNG; never URLs or symlinks."""
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    try:
        path = Path(value)
        if not path.is_absolute() or path.suffix.lower() != ".png":
            return None
        if any(part.is_symlink() for part in (path, *path.parents)):
            return None
        if not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
            return None
        from PIL import Image

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format != "PNG" or image.width * image.height > 20_000_000:
                    return None
                image.verify()
        return str(path.resolve(strict=True))
    except Exception:
        return None


def _normalise_response(raw: object) -> AssistantResponse:
    if not isinstance(raw, Mapping):
        return AssistantResponse("", error="El backend devolvió una respuesta no válida.")
    answer = raw.get("final_answer")
    if answer is not None and not isinstance(answer, str):
        return AssistantResponse("", error="El formato de respuesta no es compatible.")
    answer = (answer or "").strip() or "El backend no entregó una respuesta de texto."
    original_chart = raw.get("chart_path")
    chart = validate_chart_path(original_chart)
    error = None
    if original_chart is not None and chart is None:
        error = "La gráfica no está disponible o no es un PNG local válido."
    return AssistantResponse(answer, chart, error)


def _backend_failure(exc: Exception) -> AssistantResponse:
    """Report the failing component with a bounded, credential-free diagnosis."""
    kind = type(exc).__module__.lower()
    detail = str(exc).lower()
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        code, message = "backend_import", "Backend: dependencia o importación no disponible."
    elif "google_api_key" in detail or "api key" in detail or "configerror" in type(exc).__name__.lower():
        code, message = "model_configuration", "Modelo: configuración de la credencial no válida."
    elif "psycopg" in kind or "sqlalchemy" in kind:
        code, message = "sql_service", "PostgreSQL: falló la conexión o la consulta."
    elif "pinecone" in kind:
        code, message = "rag_service", "Documentación: falló el servicio de recuperación."
    elif "google" in kind or "ollama" in kind or "timeout" in detail or "connection" in detail:
        code, message = "model_service", "Modelo o servicio: la solicitud no pudo completarse."
    else:
        code, message = "backend_execution", "Backend: la ejecución no pudo completarse."
    safe_detail = str(exc)
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("KEY", "PASSWORD", "TOKEN", "SECRET")):
            safe_detail = safe_detail.replace(value, "[credencial omitida]")
    safe_detail = re.sub(r"\b(?:https?|postgres(?:ql)?(?:\+\w+)?)://\S+", "[dirección omitida]", safe_detail)
    safe_detail = re.sub(r"(?i)\b(?:bearer|api[_-]?key|password|token)\b\s*[:=]?\s*[^\s,;]+", "[credencial omitida]", safe_detail)
    safe_detail = " ".join(safe_detail.split())[:320]
    return AssistantResponse("", error=f"{message} {type(exc).__name__}: {safe_detail}", error_code=code)


def _invoke_existing_backend(question: str, *, thread_id: str, access_token: str) -> AssistantResponse:
    """Execute only for a verified Data Steward on pinned, authorized test sources."""
    if not is_authenticated_login(access_token):
        return AssistantResponse("", error="Inicia sesión con una identidad de prueba autorizada.", error_code="authorization")
    context = get_access_context(access_token)
    if not context:
        return AssistantResponse("", error="La autorización de pruebas no está vigente. Revisa la configuración o renueva la sesión.", error_code="authorization")
    if context["profile"] != "data_steward":
        return AssistantResponse(
            "", error="Este perfil todavía no tiene permisos reales habilitados. Se requiere aislamiento de datos en el backend.", error_code="authorization_scope"
        )
    try:
        config, _ = _session_config(_SESSIONS[access_token])
        sources = config["sources"]
        # Loading/importing the existing backend occurs only after authentication.
        # The pinned sources are checked before its first service invocation.
        from main import ask_data_steward
        from agents import sql_agent
        from tools import rag_tool

        if (
            sql_agent.PGHOST != sources["postgres_host"]
            or sql_agent.PGPORT != sources["postgres_port"]
            or sql_agent.PGDATABASE != sources["postgres_database"]
            or rag_tool.INDEX_NAME != sources["pinecone_index"]
        ):
            return AssistantResponse("", error="Las fuentes del backend no coinciden con el entorno de pruebas autorizado.", error_code="source_authorization")
        return _normalise_response(
            ask_data_steward(
                question=question, thread_id=thread_id, user_role="universal",
                cliente_id=None, auto_open_chart=False, files=None,
            )
        )
    except Exception as exc:
        return _backend_failure(exc)


def _demo_answer(question: str, profile: str) -> str:
    text = "".join(
        character for character in unicodedata.normalize("NFD", question.lower())
        if unicodedata.category(character) != "Mn"
    )
    label = "**Modo demostración · datos sintéticos, sin conexión bancaria.**\n\n"
    if profile == "cliente":
        if any(word in text for word in ("saldo", "ahorro", "grafica", "evolucion")):
            return label + (
                "Tu cuenta ficticia tiene un saldo de **$18,450.00 MXN**. "
                "El ahorro mensual de ejemplo es **$2,000.00 MXN**. "
                "Estos importes no corresponden a una cuenta real."
            )
        if any(word in text for word in ("movimiento", "transaccion", "gasto")):
            return label + (
                "Movimientos de una cuenta ficticia:\n\n"
                "| Concepto | Importe MXN |\n|---|---:|\n"
                "| Ingreso de ejemplo | +$12,000.00 |\n"
                "| Compra de ejemplo | −$650.00 |\n"
                "| Servicio de ejemplo | −$420.00 |\n\n"
                "Son operaciones ilustrativas; no forman un estado de cuenta."
            )
        if any(word in text for word in ("producto", "cuenta", "comision")):
            return label + (
                "Puedes explorar **Cuenta Demo**, un producto ficticio para "
                "ejemplos educativos de ahorro y gastos. No se ofrecen productos reales ni se "
                "afirman tasas, comisiones o condiciones comerciales de Banamex."
            )
        return label + (
            "Puedo mostrar ejemplos de saldos, movimientos, productos o ahorro. "
            "Selecciona una sugerencia para recorrer el asistente de banca personal."
        )
    if profile == "auditor":
        return label + (
            "**Revisión de controles de ejemplo**\n\n"
            "- Comprobar que cada conversación tenga su propio identificador.\n"
            "- Verificar que cambiar de perfil descarte el contexto anterior.\n"
            "- Exigir evidencia verificable antes de emitir una conclusión.\n\n"
            "El perfil Auditor tiene una vista independiente de revisión. "
            "No recibe permisos universal ni acceso real a SQL. No se han "
            "ejecutado controles ni obtenido evidencias o trazas del backend."
        )
    return label + (
        "**Análisis institucional de ejemplo**\n\n"
        "Una cartera completamente ficticia contiene **215 registros**; "
        "**8** requieren revisar datos obligatorios. Estos números solo sirven "
        "para demostrar la interfaz.\n\n"
        "En una conexión autorizada, el Hub podría coordinar análisis SQL, "
        "documentación y visualizaciones. En esta demostración no se ejecutan "
        "consultas ni se generan SQL, fuentes RAG, evidencias o trazas."
    )


def ask_assistant(
    question: str,
    *,
    thread_id: str,
    portal: str,
    profile: str,
    identity: str,
    demo_mode: bool = True,
    access_token: str | None = None,
) -> AssistantResponse:
    """Keep demo and authorized real execution separate; never fabricate a fallback."""
    allowed = {"institucional": {"data_steward", "auditor"}, "cliente": {"cliente"}}
    try:
        UUID(thread_id)
        valid_context = (
            profile in allowed.get(portal, set())
            and isinstance(identity, str) and bool(identity.strip())
        )
        if not valid_context:
            raise ValueError("invalid context")
    except (ValueError, TypeError, AttributeError):
        return AssistantResponse("", error="El contexto de la conversación no es válido.")
    if not isinstance(question, str) or not question.strip():
        return AssistantResponse("", error="Escribe una pregunta para continuar.")
    if len(question) > 4_000:
        return AssistantResponse("", error="La pregunta debe tener como máximo 4,000 caracteres.")
    if demo_mode is True:
        return AssistantResponse(_demo_answer(question.strip(), profile))
    if profile in {"cliente", "auditor"}:
        return AssistantResponse(
            "", error="Este perfil todavía no tiene permisos reales habilitados. Se requiere aislamiento de datos en el backend.", error_code="authorization_scope"
        )
    status = backend_status()
    if not status["configured"]:
        return AssistantResponse("", error=status["message"], error_code="configuration")
    if not is_authenticated_login(access_token):
        return AssistantResponse("", error="Inicia sesión con una identidad de prueba autorizada.", error_code="authorization")
    context = get_access_context(access_token)
    if context != {"identity": identity, "profile": profile, "portal": portal}:
        return AssistantResponse("", error="La identidad o los permisos de la sesión no están autorizados.", error_code="authorization")
    with _LOCK:
        owner = _THREAD_OWNERS.get(thread_id)
        if owner is not None and owner != access_token:
            return AssistantResponse("", error="La conversación pertenece a otra sesión.")
        _THREAD_OWNERS[thread_id] = access_token
        # Serialize calls: the existing backend contains process-level caches and logs.
        return _invoke_existing_backend(question.strip(), thread_id=thread_id, access_token=access_token)


def _configure_test_access() -> None:
    """Interactive operator setup; no services or backend modules are loaded."""
    import argparse
    from getpass import getpass
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser(description="Configura una identidad Data Steward solo para pruebas autorizadas.")
    parser.add_argument("--configure-test-access", required=True, type=Path, metavar="RUTA_EXTERNA")
    args = parser.parse_args()
    destination = args.configure_test_access.expanduser()
    project = Path(__file__).resolve().parents[1]
    if not destination.is_absolute() or destination.resolve().is_relative_to(project):
        parser.error("Usa una ruta absoluta fuera del repositorio para guardar la configuración.")
    if destination.exists() or not destination.parent.is_dir():
        parser.error("El archivo no debe existir y su carpeta debe existir.")
    load_dotenv(project / ".env")
    print("Se utilizarán las fuentes PostgreSQL y documentales del entorno actual.")
    if input("Escribe PRUEBAS para confirmar que todo el corpus es sintético y autorizado: ").strip() != "PRUEBAS":
        parser.error("Configuración cancelada: no se ha autorizado el entorno de pruebas.")
    username = input("Identidad Data Steward de pruebas: ").strip()
    password = getpass("Contraseña nueva de pruebas (mínimo 12 caracteres): ")
    confirmation = getpass("Repite la contraseña: ")
    if not username or len(username) > 128 or not 12 <= len(password) <= 1024 or password != confirmation:
        parser.error("Identidad o contraseña no válidas; no se creó ningún archivo.")
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000).hex()
    settings = {
        "environment": "test", "enabled": True,
        "sources": {
            "postgres_host": os.environ.get("PGHOST", "localhost"),
            "postgres_port": os.environ.get("PGPORT", "5432"),
            "postgres_database": os.environ.get("PGDATABASE", "reto_banamex"),
            "pinecone_index": os.environ.get("INDEX_NAME", "banamex-governance"),
            "entire_corpus_authorized": True,
        },
        "users": {username: {
            "profile": "data_steward",
            "password_hash": f"pbkdf2_sha256$600000${salt.hex()}${digest}",
        }},
    }
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(settings, stream, indent=2)
            stream.write("\n")
    except OSError:
        parser.error("No se pudo crear la configuración externa de pruebas.")
    print("Configuración externa creada con permisos 0600 y contraseña derivada, sin texto plano.")
    print("Define BANAMEX_TEST_ACCESS_CONFIG con la ruta del archivo antes de iniciar Streamlit.")


if __name__ == "__main__":
    _configure_test_access()

