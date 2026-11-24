"""Owned conversations and images around the existing LangGraph entry point."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import os
import re
import shutil
from pathlib import Path
from threading import RLock
from uuid import uuid4

from api.authorization import APIError, Principal, authorized_config, ensure_principal
from frontend.backend_adapter import _backend_failure, validate_chart_path


ROOT = Path(__file__).resolve().parents[2]
CHART_DIRECTORY = ROOT / "outputs" / "charts"
_LOCK = RLock()
_GRAPH_LOCK = RLock()


@dataclass(frozen=True)
class Conversation:
    owner: str
    identity: str
    profile: str
    cliente_id: int | None


_CONVERSATIONS: dict[str, Conversation] = {}
_CHARTS: dict[str, tuple[str, Path]] = {}


def _service_failure(principal: Principal, exc: Exception) -> APIError:
    failure = _backend_failure(exc)
    logging.getLogger(__name__).warning("Assistant service: %s", failure.error)
    message = (
        "El asistente no pudo conectar con el servicio de consulta. Inténtalo de nuevo más tarde."
        if principal.profile == "cliente" else failure.error or "El asistente no pudo completar la consulta."
    )
    return APIError(503, failure.error_code or "assistant_service", message)


def _discard_failed_answer(graph, conversation_id: str, state: dict) -> None:
    """Keep an upstream failure from becoming financial context for a follow-up."""
    from langchain_core.messages import AIMessage

    messages = state.get("messages") or []
    if messages and isinstance(messages[-1], AIMessage) and messages[-1].id:
        replacement = AIMessage(
            id=messages[-1].id, name=messages[-1].name,
            content="La consulta anterior no pudo completarse. No hay resultados verificados de ese turno.",
        )
        graph.update_state(
            {"configurable": {"thread_id": conversation_id}},
            {"messages": [replacement], "final_answer": None, "chart_path": None},
        )


def create_conversation(principal: Principal) -> dict:
    ensure_principal(principal, "assistant:use")
    conversation_id = str(uuid4())
    with _LOCK:
        _CONVERSATIONS[conversation_id] = Conversation(
            principal.session_id, principal.identity, principal.profile, principal.cliente_id,
        )
    return {"conversation_id": conversation_id}


def _owned_conversation(principal: Principal, conversation_id: str) -> Conversation:
    ensure_principal(principal, "assistant:use")
    with _LOCK:
        conversation = _CONVERSATIONS.get(conversation_id)
    if not conversation or conversation.owner != principal.session_id:
        raise APIError(404, "conversation_not_found", "La conversación no está disponible para tu sesión.")
    if (conversation.identity, conversation.profile, conversation.cliente_id) != (principal.identity, principal.profile, principal.cliente_id):
        raise APIError(403, "conversation_scope", "La conversación no corresponde a tu identidad actual.")
    return conversation


def _client_text(answer: str) -> str:
    """Preserve business paragraphs and numbering; omit actual technical blocks."""
    answer = re.sub(r"```[\s\S]*?```", "", answer)
    sql_statement = re.compile(
        r"(?:SELECT\b[\s\S]*?\bFROM\s+\S+|SELECT\s+(?:\d+|\*)\s*;?\s*$|WITH\s+(?:RECURSIVE\s+)?[\w\"]+\s+AS\s*\()",
        re.IGNORECASE,
    )
    answer = re.sub(
        r"`([^`\n]+)`",
        lambda match: "[consulta técnica omitida]" if sql_statement.match(match[1].strip()) else match[0],
        answer,
    )
    source_lines = answer.splitlines()
    lines = []
    skip_until = 0
    for index, line in enumerate(source_lines):
        if index < skip_until:
            continue
        if re.search(r"^\s*(?:\*\*)?(?:INFORME NORMATIVO OFICIAL|A:|DE:|ASUNTO:)", line, re.IGNORECASE):
            continue
        if re.match(r"^\s*(?:(?:SQL\s*:\s*)?(?:SELECT|WITH)\b|Traceback\s*\(most recent call last\))", line, re.IGNORECASE):
            end = index
            while end < len(source_lines) and source_lines[end].strip():
                if end > index and re.match(r"^\s*(?:#{1,6}\s+|\d+[.)]\s+)", source_lines[end]):
                    break
                end += 1
                if source_lines[end - 1].rstrip().endswith(";"):
                    break
            block = re.sub(r"^\s*SQL\s*:\s*", "", "\n".join(source_lines[index:end]), flags=re.IGNORECASE).strip()
            if sql_statement.match(block) or block.startswith("Traceback (most recent call last)"):
                skip_until = end
                continue
        if re.match(r'^\s*(?:File\s+"[^\"]+",\s+line\s+\d+|[\w.]+(?:Error|Exception)\s*:)', line):
            continue
        if re.match(r"^\s*#{1,6}\s*(?:SQL(?:\s+(?:generad[oa]|ejecutad[oa]|intern[oa]))?|Traceback|(?:Trazas?|Detalles)\s+(?:intern[oa]s?|t[eé]cnic[oa]s?))\s*:?\s*$", line, re.IGNORECASE):
            continue
        query = sql_statement.search(line)
        if query:
            line = line[:query.start()] + "[consulta técnica omitida]"
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n\s*\*{0,2}(?:Atentamente|Cordialmente)[\s\S]*$", "", cleaned, flags=re.IGNORECASE).strip()


def _register_chart(principal: Principal, value: object) -> str | None:
    if value is None:
        return None
    valid = validate_chart_path(value)
    if not valid:
        raise APIError(502, "invalid_chart", "La respuesta contiene una gráfica que no se puede visualizar de forma segura.")
    path = Path(valid)
    if not path.is_relative_to(CHART_DIRECTORY.resolve()):
        raise APIError(502, "invalid_chart", "La gráfica no pertenece al directorio de resultados autorizado.")
    chart_id = str(uuid4())
    # Snapshot the generated result: a later backend execution must not replace
    # bytes belonging to an earlier session even if the tool reuses a filename.
    private_directory = CHART_DIRECTORY / "web"
    private_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if private_directory.is_symlink():
        raise APIError(502, "invalid_chart", "El directorio de gráficas no es válido.")
    owned_path = private_directory / f"{chart_id}.png"
    descriptor = os.open(owned_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
        shutil.copyfileobj(source, target)
    with _LOCK:
        _CHARTS[chart_id] = (principal.session_id, owned_path)
    return chart_id


def get_chart(principal: Principal, chart_id: str) -> Path:
    ensure_principal(principal, "assistant:use")
    with _LOCK:
        chart = _CHARTS.get(chart_id)
    if not chart or chart[0] != principal.session_id:
        raise APIError(404, "chart_not_found", "La gráfica no está disponible para tu sesión.")
    valid = validate_chart_path(chart[1])
    if not valid or not Path(valid).is_relative_to(CHART_DIRECTORY.resolve()):
        raise APIError(404, "chart_not_found", "La gráfica ya no está disponible.")
    return Path(valid)


def ask(principal: Principal, conversation_id: str, question: str, *, files: list[str] | None = None) -> dict:
    _owned_conversation(principal, conversation_id)
    if not isinstance(question, str) or (not question.strip() and not files) or len(question) > 4000:
        raise APIError(400, "invalid_question", "Escribe una pregunta de entre 1 y 4,000 caracteres.")
    if principal.profile not in {"data_steward", "cliente"}:
        raise APIError(403, "assistant_role", "El asistente aún no tiene una política específica para el perfil Auditor.")
    if principal.profile == "cliente":
        requested = re.findall(r"(?:cliente|num_cte|id\s+de\s+cliente)\s*(?:#|=|:)?\s*(\d+)", question, re.IGNORECASE)
        if any(int(value) != principal.cliente_id for value in requested):
            raise APIError(403, "client_scope", "Solo puedes consultar información de tu propia identidad autorizada.")
        try:
            from tools.sql_scope import CLIENT_SCOPE_ENFORCED
        except ImportError as exc:
            raise APIError(503, "client_scope_pending", "La protección de consultas por cliente todavía no está disponible.") from exc
        if CLIENT_SCOPE_ENFORCED is not True:
            raise APIError(503, "client_scope_pending", "La protección de consultas por cliente todavía no está disponible.")
    config, _ = authorized_config()
    source_summary = None
    if principal.cliente_id is not None:
        try:
            from api.services.banking import get_summary

            source_summary = get_summary(principal.cliente_id)
        except Exception:
            # Documentary queries may still work while the banking source is down.
            pass
    scoped_question = question.strip()
    if principal.profile == "cliente":
        scoped_question += (
            "\nContexto verificado del portal: responde en lenguaje de banca personal, "
            "sin código, nombres de tablas, trazas internas ni certificaciones oficiales. "
            "Los saldos de esta fuente son registrados, no disponibles."
        )
        if source_summary and source_summary.get("cutoff_date"):
            scoped_question += (
                f" El último cierre disponible para esta identidad es {source_summary['cutoff_date']}. "
                "Para saldos actuales usa solo ese cierre; para evolución histórica muestra cada cierre por separado, "
                "sin sumar fotografías mensuales de un saldo."
            )
        if source_summary and source_summary.get("currency") is None:
            scoped_question += (
                " La fuente de saldos no especifica moneda: no inventes pesos, MXN, "
                "símbolos monetarios ni unidades en texto o gráficas de saldos. "
                "La moneda de una tarifa documental solo se indica si aparece en su documento."
            )
    try:
        from main import DEFAULT_UPLOAD_QUESTION, ask_data_steward, multi_agent_app
        from agents import sql_agent
        from tools import rag_tool
    except Exception as exc:
        raise _service_failure(principal, exc) from exc
    if files and not question.strip():
        scoped_question = DEFAULT_UPLOAD_QUESTION + scoped_question
    sources = config["sources"]
    if (sql_agent.PGHOST, sql_agent.PGPORT, sql_agent.PGDATABASE, rag_tool.INDEX_NAME) != (
        sources["postgres_host"], sources["postgres_port"], sources["postgres_database"], sources["pinecone_index"],
    ):
        raise APIError(503, "source_authorization", "Las fuentes del asistente no coinciden con las fuentes de prueba autorizadas.")
    try:
        with _GRAPH_LOCK:
            ensure_principal(principal, "assistant:use")
            from tools.chart_context import chart_units

            units_token = chart_units.set((True, source_summary.get("currency") if source_summary else None))
            try:
                result = ask_data_steward(
                    scoped_question, thread_id=conversation_id,
                    user_role="cliente" if principal.profile == "cliente" else "universal",
                    cliente_id=principal.cliente_id, auto_open_chart=False, files=files,
                )
            finally:
                chart_units.reset(units_token)
            state = multi_agent_app.get_state({"configurable": {"thread_id": conversation_id}}).values
            for evidence_kind, evidence in (state.get("evidences") or {}).items():
                if isinstance(evidence, dict) and evidence.get("error"):
                    _discard_failed_answer(multi_agent_app, conversation_id, state)
                    if evidence["error"] == "ACCESS_DENIED":
                        raise APIError(403, "assistant_scope", "La consulta no está permitida para tu identidad autorizada.")
                    diagnosis = _backend_failure(RuntimeError(str(evidence["error"])))
                    logging.getLogger(__name__).warning("%s: %s", evidence_kind, diagnosis.error)
                    if evidence_kind == "rag_evidence":
                        if "ollama" in str(evidence["error"]).lower():
                            detail = "El servicio local de embeddings Ollama no responde. Inicia Ollama y verifica el modelo de embeddings configurado."
                        else:
                            detail = "No se pudo completar la consulta documental. Revisa la disponibilidad de las fuentes y del modelo."
                        if principal.profile == "cliente":
                            detail = "La consulta documental no está disponible temporalmente. Puedes reintentarlo más tarde."
                        raise APIError(503, "document_service", detail)
                    if evidence_kind in {"doc_evidence", "ocr_evidence"}:
                        raise APIError(503, "document_processing", "No fue posible procesar el documento. Revisa que sea legible y vuelve a intentarlo.")
                    raise APIError(503, "financial_service", "No fue posible completar la consulta de información financiera. No se generó una respuesta simulada.")
            # Snapshot before another graph execution can overwrite a result file.
            chart_id = _register_chart(principal, result.get("chart_path")) if isinstance(result, dict) else None
    except APIError:
        raise
    except Exception as exc:
        raise _service_failure(principal, exc) from exc
    if not isinstance(result, dict) or not isinstance(result.get("final_answer"), str) or not result["final_answer"].strip():
        raise APIError(502, "empty_assistant_answer", "El asistente no devolvió una respuesta de texto. No se generó una respuesta simulada.")
    answer = _client_text(result["final_answer"]) if principal.profile == "cliente" else result["final_answer"].strip()
    # Only the separately authorized chart endpoint may supply rendered images.
    answer = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", answer)
    answer = re.sub(
        r"(?<!\w)(?:file://\S+|/(?:Users|private|home|tmp|var)/[^\s)\]`]+|[A-Za-z]:\\[^\s)\]`]+)",
        "[archivo de la conversación]", answer,
    )
    for name, value in os.environ.items():
        if value and any(word in name.upper() for word in ("KEY", "PASSWORD", "TOKEN", "SECRET")):
            answer = answer.replace(value, "[credencial omitida]")
    if source_summary and source_summary.get("currency") is None and (state.get("evidences") or {}).get("sql_evidence"):
        answer = re.sub(r"\b(?:MXN|pesos mexicanos)\b|\$", "", answer, flags=re.IGNORECASE)
        answer = answer.replace("()", "").strip() + "\n\n*La fuente no especifica moneda ni saldo disponible.*"
    if not answer:
        raise APIError(502, "assistant_answer_format", "La respuesta del asistente no contiene información que pueda presentarse en este portal.")
    cutoff_date = source_summary.get("cutoff_date") if source_summary else None
    return {"final_answer": answer, "chart_id": chart_id, "cutoff_date": cutoff_date}


def clear_session(session_id: str) -> None:
    with _LOCK:
        for key in [key for key, item in _CONVERSATIONS.items() if item.owner == session_id]:
            _CONVERSATIONS.pop(key, None)
        for key in [key for key, item in _CHARTS.items() if item[0] == session_id]:
            _CHARTS.pop(key, None)
