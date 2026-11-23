"""Diagnósticos públicos y accionables sin copiar excepciones del servicio."""

from __future__ import annotations

import re
from typing import Any


ERROR_CODES = frozenset({
    "MODEL_NOT_FOUND", "QUOTA_EXCEEDED", "AUTHENTICATION_ERROR", "PERMISSION_DENIED",
    "SQL_SCHEMA_ERROR", "SQL_SYNTAX_ERROR", "DATABASE_CONNECTION_ERROR", "SOURCE_UNAVAILABLE",
})


def _field(value: Any, name: str) -> Any:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _error_chain(error: Any) -> list[Any]:
    """Include SQLAlchemy's original driver exception, without following cycles."""
    chain, pending, seen = [], [error], set()
    while pending and len(chain) < 8:
        current = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        pending.extend(_field(current, name) for name in ("orig", "__cause__", "__context__"))
    return chain


def _status_number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
        return value
    if isinstance(value, str):
        if value.isdigit() and 400 <= int(value) <= 599:
            return int(value)
        return {
            "NOT_FOUND": 404, "RESOURCE_EXHAUSTED": 429,
            "UNAUTHENTICATED": 401, "PERMISSION_DENIED": 403,
        }.get(value.upper())
    enum_name = _field(value, "name")
    return _status_number(enum_name) if isinstance(enum_name, str) else None


def _classify(error: Any) -> str:
    # Canonical codes can safely cross the graph/UI boundary more than once.
    if isinstance(error, str) and error in ERROR_CODES:
        return error
    chain = _error_chain(error)
    texts = " ".join(str(item)[:8000] for item in chain).lower()
    for item in chain:
        pgcode = _field(item, "pgcode") or _field(item, "sqlstate")
        if pgcode in {"42703", "42P01"}:
            return "SQL_SCHEMA_ERROR"
        if pgcode == "42601":
            return "SQL_SYNTAX_ERROR"

    statuses = set()
    for item in chain:
        values = [_field(item, name) for name in ("status_code", "status", "code")]
        values.append(_field(_field(item, "response"), "status_code"))
        statuses.update(number for value in values if (number := _status_number(value)) is not None)
    # Legacy backends sometimes retain only str(exception), such as "404 NOT_FOUND".
    for pattern in (
        r"\b(?:http(?:error)?|status(?:_code)?|error\s+code|code)\s*[:=]?\s*(401|403|404|429)\b",
        r"\b(401|403|404|429)\s+(?:not_found|not\s+found|resource_exhausted|unauthenticated|permission_denied|client\s+error|unauthorized|forbidden|too\s+many\s+requests)\b",
        r"['\"]code['\"]\s*:\s*(401|403|404|429)\b",
    ):
        statuses.update(int(match) for match in re.findall(pattern, texts))
    if 429 in statuses or re.search(r"\b(?:resource_exhausted|quota[_ ]exceeded|rate[_ ]limit(?:ed|[_ ]exceeded)?)\b", texts):
        return "QUOTA_EXCEEDED"
    if 401 in statuses or re.search(r"\b(?:api[_ ]key[_ ]invalid|invalid[_ ]api[_ ]key|api key not valid|unauthenticated)\b", texts):
        return "AUTHENTICATION_ERROR"
    if 403 in statuses or "permission_denied" in texts:
        return "PERMISSION_DENIED"
    if 404 in statuses or ("model" in texts and re.search(
        r"not[_ ]found|not supported|does not exist|invalid model|unexpected model name", texts
    )):
        return "MODEL_NOT_FOUND"
    if re.search(r"\b(?:42703|42p01|undefinedcolumn|undefinedtable)\b", texts):
        return "SQL_SCHEMA_ERROR"
    if re.search(r"\b42601\b|syntax error at or near", texts):
        return "SQL_SYNTAX_ERROR"
    if any("operationalerror" in type(item).__name__.lower() for item in chain) or re.search(
        r"connection (?:refused|failed|timed out)|could not connect|could not translate host name|"
        r"psycopg\d?\.operationalerror|library not loaded.*libpq", texts
    ):
        return "DATABASE_CONNECTION_ERROR"
    return "SOURCE_UNAVAILABLE"


def public_service_error(error: Any, source: str = "sql") -> dict[str, str]:
    """Map an exception or legacy error string to an allowlisted diagnosis.

    Returned messages use static configuration names only. Exception text,
    request URLs, credentials and generated SQL are never included.
    """
    model_setting = {"sql": "SQL_MODEL", "rag": "RAG_MODEL"}.get(source, "el modelo configurado")
    messages = {
        "MODEL_NOT_FOUND": (
            f"El modelo solicitado no está disponible. Revisa {model_setting} y utiliza un modelo Gemini "
            "válido y habilitado para tu proyecto. Reinicia la aplicación después de cambiar el .env."
        ),
        "QUOTA_EXCEEDED": (
            "El servicio de IA alcanzó su cuota o límite de solicitudes. Espera antes de reintentar "
            "y revisa la cuota o facturación del proyecto de Google."
        ),
        "AUTHENTICATION_ERROR": (
            "El servicio rechazó la autenticación. Revisa GOOGLE_API_KEY y la configuración de credenciales "
            "del servicio utilizado. Reinicia la aplicación si cambiaste el .env."
        ),
        "PERMISSION_DENIED": (
            "El servicio rechazó el acceso por permisos. Revisa que la credencial y el proyecto "
            "tengan acceso al modelo o recurso solicitado."
        ),
        "SQL_SCHEMA_ERROR": (
            "La consulta SQL usa una tabla o columna que PostgreSQL no reconoce. Revisa la consulta "
            "generada y su correspondencia con el esquema de la base de datos."
        ),
        "SQL_SYNTAX_ERROR": (
            "PostgreSQL rechazó la sintaxis del SQL generado. Revisa la consulta y vuelve a solicitar "
            "el análisis con una instrucción más precisa."
        ),
        "DATABASE_CONNECTION_ERROR": (
            "No fue posible conectar con PostgreSQL. Comprueba que el servidor esté activo y revisa "
            "PGHOST, PGPORT, PGDATABASE, PGUSER y PGPASSWORD."
        ),
        "SOURCE_UNAVAILABLE": (
            "La fuente no pudo completar la consulta. Comprueba su configuración y disponibilidad "
            "antes de volver a intentarlo."
        ),
    }
    code = _classify(error)
    return {"code": code, "message": messages[code]}
