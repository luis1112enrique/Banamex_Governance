"""Streamlit adapter for the existing Hub-and-Spoke graph.

This module has no Streamlit dependency. It keeps a per-run audit trail rather
than reading a checkpoint that could contain evidence from another question.
"""

from __future__ import annotations

import ast
import copy
import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from dotenv import load_dotenv

from service_errors import public_service_error


PROJECT_ROOT = Path(__file__).resolve().parent
CHARTS_ROOT = PROJECT_ROOT / "outputs" / "charts"
RUN_ERROR = "No fue posible completar la consulta. Revisa la configuración y disponibilidad de los servicios e inténtalo de nuevo."


def _load_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


def _safe_evidence(data: Any, source: str = "sql") -> dict[str, Any]:
    """Do not expose raw exception strings produced by a backend source."""
    result = copy.deepcopy(data) if isinstance(data, dict) else {"summary": str(data)}
    if result.get("error") and result["error"] != "ACCESS_DENIED":
        diagnostic = public_service_error(result["error"], source=source)
        result["error"] = diagnostic["code"]
        result["summary"] = diagnostic["message"]
        result["diagnostic"] = diagnostic
    return result


def run_data_steward(
    question: str,
    thread_id: str,
    user_role: str,
    cliente_id: int | None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
    *,
    graph: Any = None,
) -> dict[str, Any]:
    """Run the graph and collect answer, chart and every source invocation.

    ``on_step`` receives one orchestration record per graph update. ``graph``
    supports dependency injection for local tests without external services.
    The caller must issue a new thread when the selected identity changes.
    """
    result: dict[str, Any] = {
        "final_answer": None,
        "chart_path": None,
        "evidences": [],
        "orchestration": [],
    }
    if (
        not isinstance(question, str)
        or not question.strip()
        or not isinstance(thread_id, str)
        or not thread_id.strip()
        or user_role not in {"universal", "cliente"}
        or (
            user_role == "cliente"
            and (isinstance(cliente_id, bool) or not isinstance(cliente_id, int) or cliente_id <= 0)
        )
        or (user_role == "universal" and cliente_id is not None)
    ):
        result["error"] = "La consulta o el perfil de sesión no son válidos. Inicia una nueva conversación y revisa el identificador de cliente."
        return result

    try:
        _load_environment()
        from langchain_core.messages import HumanMessage

        if graph is None:
            from workflow import multi_agent_app

            graph = multi_agent_app

        initial_input = {
            "messages": [HumanMessage(content=question.strip())],
            "question": question.strip(),
            "user_role": user_role,
            "cliente_id": cliente_id,
            "plan": None,
            "next_agent": None,
            "spoke_instruction": None,
            "evidences": {},
            "uploaded_files": [],
            "chart_path": None,
            "step_count": 0,
            "final_answer": None,
        }
        config = {
            "configurable": {"thread_id": thread_id},
            "run_name": "Banamex Data Steward | Streamlit",
            "tags": [f"role:{user_role}", "hub-and-spoke", "streamlit"],
            "metadata": {
                "thread_id": thread_id,
                "user_role": user_role,
                "cliente_id": cliente_id,
            },
        }
        for event in graph.stream(initial_input, config=config, stream_mode="updates"):
            for node, update in event.items():
                if not isinstance(update, dict):
                    continue
                record = {
                    "node": node,
                    "plan": update.get("plan"),
                    "next_agent": update.get("next_agent"),
                    "instruction": update.get("spoke_instruction"),
                    "step_count": update.get("step_count"),
                }
                result["orchestration"].append(record)
                for name, data in (update.get("evidences") or {}).items():
                    result["evidences"].append({
                        "node": node,
                        "name": name,
                        "data": _safe_evidence(data, source={"sql_agent": "sql", "rag_agent": "rag"}.get(node, "backend")),
                    })
                if update.get("final_answer"):
                    result["final_answer"] = str(update["final_answer"])
                if update.get("chart_path"):
                    resolved = resolve_chart_path(update["chart_path"])
                    result["chart_path"] = str(resolved) if resolved else None
                if on_step is not None:
                    on_step(copy.deepcopy(record))
        if not result["final_answer"]:
            result["error"] = "El coordinador terminó sin emitir un dictamen. Puedes revisar los pasos y las evidencias disponibles e intentar de nuevo."
    except Exception as exc:
        diagnostic = public_service_error(exc, source="backend")
        result["error"] = RUN_ERROR if diagnostic["code"] == "SOURCE_UNAVAILABLE" else diagnostic["message"]
        result["error_code"] = diagnostic["code"]
    return result


def _decode_sql_literal(node: ast.AST, depth: int = 0) -> Any:
    """Interpret a tiny SQLDatabase repr grammar without evaluating Python."""
    if depth > 60:
        raise ValueError("SQL result nesting exceeds the display limit")
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_decode_sql_literal(value, depth + 1) for value in node.elts]
    if isinstance(node, ast.Dict):
        return {
            _decode_sql_literal(key, depth + 1): _decode_sql_literal(value, depth + 1)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _decode_sql_literal(node.operand, depth + 1)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Call) and not node.keywords:
        args = [_decode_sql_literal(arg, depth + 1) for arg in node.args]
        if isinstance(node.func, ast.Name) and node.func.id == "Decimal":
            if len(args) == 1 and isinstance(args[0], str) and len(args[0]) < 1000:
                return str(Decimal(args[0]))
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "datetime"
            and all(isinstance(value, int) and not isinstance(value, bool) for value in args)
        ):
            constructors = {
                "date": datetime.date,
                "datetime": datetime.datetime,
                "time": datetime.time,
            }
            constructor = constructors.get(node.func.attr)
            if constructor is not None:
                return constructor(*args).isoformat()
    raise ValueError("Unsupported SQL result representation")


def decode_sql_result(raw: Any) -> list[dict[str, Any]] | None:
    """Convert JSON/Python literal rows to records without executing code.

    Decimal and datetime reprs are converted by a restricted AST interpreter.
    It accepts only literals and those explicitly named constructors; arbitrary
    calls, attributes, operators and comprehensions are rejected.
    """
    if isinstance(raw, str):
        if len(raw) > 1_000_000 or not raw.strip():
            return None
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                try:
                    expression = ast.parse(raw, mode="eval")
                    if sum(1 for _ in ast.walk(expression)) > 10_000:
                        return None
                    raw = _decode_sql_literal(expression.body)
                except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError, InvalidOperation, OverflowError):
                    return None
    if isinstance(raw, dict):
        return [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    if not raw:
        return []
    if all(isinstance(row, dict) for row in raw):
        return list(raw)
    if all(isinstance(row, (list, tuple)) for row in raw):
        return [
            {f"columna_{index + 1}": value for index, value in enumerate(row)}
            for row in raw
        ]
    if all(not isinstance(row, (dict, list, tuple)) for row in raw):
        return [{"valor": row} for row in raw]
    return None


def resolve_chart_path(path: Any) -> Path | None:
    """Accept only actual PNG images inside this project's chart directory."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        return None
    try:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidate = candidate.resolve(strict=True)
        candidate.relative_to(CHARTS_ROOT.resolve())
        if not candidate.is_file() or candidate.suffix.lower() != ".png":
            return None
        from PIL import Image

        with Image.open(candidate) as chart:
            if chart.format != "PNG":
                return None
            chart.verify()
        return candidate
    except (OSError, ValueError, RuntimeError):
        return None


def _status(state: str, label: str, detail: str) -> dict[str, str]:
    return {"state": state, "label": label, "detail": detail}


def _postgresql_status() -> dict[str, str]:
    connection = None
    try:
        import psycopg2

        connection = psycopg2.connect(
            host=os.environ.get("PGHOST", "localhost"),
            port=os.environ.get("PGPORT", "5432"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", "banamex_database"),
            dbname=os.environ.get("PGDATABASE", "reto_banamex"),
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=3000",
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                return _status("error", "Sin respuesta", "La comprobación de lectura no devolvió el resultado esperado.")
        return _status("ok", "Activo", "Conexión PostgreSQL verificada con SELECT 1 de solo lectura.")
    except Exception:
        return _status("error", "No disponible", "No fue posible verificar PostgreSQL. Revisa el servidor y la configuración PG del proyecto.")
    finally:
        if connection is not None:
            connection.close()


def _pinecone_status() -> dict[str, str]:
    api_key = os.environ.get("PINECONE_API_KEY", "").strip()
    if not api_key:
        return _status("warning", "Sin configurar", "Falta PINECONE_API_KEY.")
    try:
        import requests

        name = os.environ.get("INDEX_NAME", "banamex-governance")
        response = requests.get(
            f"https://api.pinecone.io/indexes/{quote(name, safe='')}",
            headers={"Api-Key": api_key},
            timeout=(2, 3),
        )
        if response.status_code in (401, 403):
            return _status("error", "Acceso rechazado", "Pinecone rechazó la autenticación o los permisos del índice.")
        response.raise_for_status()
        if not response.json().get("status", {}).get("ready"):
            return _status("warning", "Índice no listo", "Pinecone respondió, pero el índice todavía no está listo.")
        return _status("ok", "Conectado", "Autenticación e índice listos. No se ejecutó una búsqueda ni se verificaron embeddings.")
    except Exception:
        return _status("error", "No disponible", "No fue posible verificar el índice de Pinecone. Revisa la configuración y la conexión.")


def _first_environment_value(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _langsmith_status() -> dict[str, str]:
    enabled = _first_environment_value(
        "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING"
    ).lower() in {"true", "1", "yes", "on"}
    api_key = _first_environment_value("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")
    if not enabled:
        return _status("warning", "Trazabilidad desactivada", "El envío de trazas no está habilitado en la configuración efectiva.")
    if not api_key:
        return _status("warning", "Sin credencial", "Tracing está habilitado, pero falta la clave de LangSmith.")
    endpoint = _first_environment_value("LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT") or "https://api.smith.langchain.com"
    if not endpoint.startswith("https://"):
        return _status("warning", "Sin verificar", "La comprobación de autenticación requiere un endpoint HTTPS.")
    try:
        import requests

        headers = {"x-api-key": api_key}
        workspace = os.environ.get("LANGSMITH_WORKSPACE_ID", "").strip()
        if workspace:
            headers["X-Tenant-Id"] = workspace
        response = requests.get(f"{endpoint.rstrip('/')}/settings", headers=headers, timeout=(2, 3))
        if response.status_code in (401, 403):
            return _status("error", "Acceso rechazado", "LangSmith rechazó la autenticación o los permisos del workspace.")
        response.raise_for_status()
        if not isinstance(response.json(), dict):
            return _status("warning", "Sin verificar", "El endpoint respondió sin una configuración verificable.")
        return _status("ok", "Trazabilidad activa", "Tracing habilitado y autenticación verificada por GET. No se enviaron trazas de prueba.")
    except Exception:
        return _status("warning", "Configurada, sin verificar", "Tracing está habilitado, pero no se pudo verificar la conexión autenticada.")


def get_service_statuses() -> dict[str, dict[str, str]]:
    """Read-only, bounded service checks. Never return credentials or errors."""
    _load_environment()
    checks = {
        "PostgreSQL": _postgresql_status,
        "Pinecone": _pinecone_status,
        "LangSmith": _langsmith_status,
    }
    statuses = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {name: executor.submit(check) for name, check in checks.items()}
        for name, future in futures.items():
            try:
                statuses[name] = future.result()
            except Exception:
                statuses[name] = _status("warning", "Sin verificar", "No fue posible completar la comprobación del servicio.")
    return statuses
