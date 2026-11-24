"""Local presentation identities, reusing the existing protected test-user store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import secrets
from threading import RLock
import time

from dotenv import load_dotenv

from frontend.backend_adapter import _password_matches, _test_config


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Principal:
    session_id: str
    identity: str
    display_name: str
    profile: str
    portal: str
    cliente_id: int | None
    permissions: tuple[str, ...]
    csrf_token: str
    config_fingerprint: str


@dataclass(frozen=True)
class LoginResult:
    token: str
    principal: Principal


_LOCK = RLock()
_SESSIONS: dict[str, tuple[Principal, float]] = {}
_SESSION_IDS: dict[str, str] = {}
_FAILURES: dict[str, list[float]] = {}
SESSION_TTL = 3600
_PERMISSIONS = {
    "data_steward": ("institution:read", "assistant:use", "evidence:read"),
    "auditor": ("institution:read", "evidence:read"),
    "cliente": ("banking:read", "assistant:use"),
}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authorized_config() -> tuple[dict, str]:
    """Validate the operator's test attestation and pin the configured sources."""
    try:
        config, fingerprint = _test_config()
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise APIError(503, "access_configuration", "La configuración protegida de identidades de prueba no está disponible.") from exc
    sources = config["sources"]
    expected = {
        "postgres_host": os.getenv("PGHOST", "localhost"),
        "postgres_port": os.getenv("PGPORT", "5432"),
        "postgres_database": os.getenv("PGDATABASE", "reto_banamex"),
        "pinecone_index": os.getenv("INDEX_NAME", "banamex-governance"),
    }
    if any(sources.get(key) != value for key, value in expected.items()):
        raise APIError(503, "source_authorization", "Las fuentes configuradas no coinciden con las fuentes de prueba autorizadas.")
    return config, fingerprint


def login(username: str, password: str, portal: str) -> LoginResult:
    if not isinstance(username, str) or not isinstance(password, str) or not 1 <= len(username.strip()) <= 128 or not 1 <= len(password) <= 1024:
        raise APIError(401, "invalid_credentials", "Usuario o contraseña de pruebas incorrectos.")
    if portal not in {"cliente", "institucional"}:
        raise APIError(400, "invalid_portal", "Selecciona un portal válido.")
    config, fingerprint = authorized_config()
    username = username.strip()
    now = time.monotonic()
    with _LOCK:
        failures = [stamp for stamp in _FAILURES.get(username, []) if now - stamp < 60]
        _FAILURES[username] = failures
        if len(failures) >= 5:
            raise APIError(429, "login_rate_limit", "Demasiados intentos. Espera un minuto e inténtalo nuevamente.")
    user = config["users"].get(username, {})
    profile = user.get("profile")
    expected_portal = "cliente" if profile == "cliente" else "institucional"
    if not _password_matches(password, user.get("password_hash", "")) or profile not in _PERMISSIONS or portal != expected_portal:
        with _LOCK:
            _FAILURES[username].append(now)
        raise APIError(401, "invalid_credentials", "Usuario, contraseña o portal de pruebas incorrectos.")
    cliente_id = user.get("cliente_id") if profile == "cliente" else None
    if profile == "cliente" and (type(cliente_id) is not int or cliente_id <= 0):
        raise APIError(403, "client_identity_missing", "La cuenta de pruebas no tiene un cliente autorizado asignado en el servidor.")
    display_name = user.get("display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = username
    principal = Principal(
        session_id=secrets.token_urlsafe(24), identity=username,
        display_name=display_name.strip()[:128], profile=profile, portal=portal,
        cliente_id=cliente_id, permissions=_PERMISSIONS[profile],
        csrf_token=secrets.token_urlsafe(32), config_fingerprint=fingerprint,
    )
    token = secrets.token_urlsafe(40)
    with _LOCK:
        _FAILURES.pop(username, None)
        _SESSIONS[_token_hash(token)] = (principal, now + SESSION_TTL)
        _SESSION_IDS[principal.session_id] = _token_hash(token)
    return LoginResult(token, principal)


def require_session(token: str | None) -> Principal:
    if not isinstance(token, str) or not token or len(token) > 256:
        raise APIError(401, "session_required", "Inicia sesión con una identidad de pruebas.")
    token_hash = _token_hash(token)
    with _LOCK:
        record = _SESSIONS.get(token_hash)
    if record is None:
        raise APIError(401, "session_expired", "Tu sesión de pruebas ha vencido. Inicia sesión nuevamente.")
    principal, expires = record
    _, fingerprint = authorized_config()
    if expires < time.monotonic() or principal.config_fingerprint != fingerprint:
        logout(token)
        raise APIError(401, "session_expired", "Tu sesión ha vencido o cambiaron sus permisos. Inicia sesión nuevamente.")
    return principal


def ensure_principal(principal: Principal, permission: str | None = None) -> None:
    """Also protect service calls made directly instead of through an HTTP route."""
    with _LOCK:
        token_hash = _SESSION_IDS.get(principal.session_id)
        record = _SESSIONS.get(token_hash or "")
    _, fingerprint = authorized_config()
    if not record or record[0] != principal or record[1] < time.monotonic() or fingerprint != principal.config_fingerprint:
        raise APIError(401, "session_expired", "La sesión de pruebas no está vigente.")
    if permission and permission not in principal.permissions:
        raise APIError(403, "permission_denied", "Tu perfil no tiene permiso para esta operación.")


def logout(token: str | None) -> None:
    if not isinstance(token, str):
        return
    with _LOCK:
        record = _SESSIONS.pop(_token_hash(token), None)
        if record:
            _SESSION_IDS.pop(record[0].session_id, None)


def public_session(principal: Principal) -> dict:
    ensure_principal(principal)
    return {
        "identity": principal.identity, "display_name": principal.display_name,
        "profile": principal.profile, "portal": principal.portal,
        "cliente_id": principal.cliente_id, "permissions": list(principal.permissions),
        "csrf_token": principal.csrf_token,
    }
