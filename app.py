"""Interfaz web Banamex Data Steward. Ejecutar: uv run streamlit run app.py."""

from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import streamlit as st

from service_errors import public_service_error
from ui_backend import (
    decode_sql_result,
    get_service_statuses,
    resolve_chart_path,
    run_data_steward,
)


PROFILES = ("Usuario Universal (Data Steward / Auditor)", "Cliente Banamex")
NODE_LABELS = {
    "coordinator": "Coordinador Hub",
    "sql_agent": "SQL / Base de Datos",
    "rag_agent": "RAG / Normativa",
    "chart_tool": "Visualización financiera",
    "FINISH": "Dictamen final",
}

CSS = """
<style>
    :root { --navy: #002D72; --blue: #0076CE; --gold: #FFB81C; --bg: #F4F6F9; }
    .stApp { background: var(--bg); }
    .block-container { max-width: 1250px; padding-top: 2rem; padding-bottom: 4rem; }
    [data-testid="stSidebar"] { background: #fff; border-right: 1px solid #dce4ef; }
    [data-testid="stSidebar"] .block-container { padding-top: 2rem; }
    h1, h2, h3 { color: var(--navy); }
    .brand { color: var(--navy); font-size: 1.65rem; font-weight: 850; letter-spacing: -.06em; }
    .brand-dot { color: var(--gold); }
    .eyebrow { color: #526783; font-size: .68rem; font-weight: 750; letter-spacing: .15em; }
    .hero {
        background: var(--navy); border-radius: 18px; padding: 28px 32px;
        border-top: 4px solid var(--gold); margin-bottom: 22px;
        box-shadow: 0 8px 28px #002d7212;
    }
    .hero .eyebrow { color: #a9c9ef; }
    .hero h1 { color: #fff; font-size: 1.65rem; font-weight: 700; line-height: 1.35; margin: 8px 0; }
    .hero p { color: #d9e8fb; font-size: .94rem; margin: 0; }
    .badge {
        display: inline-block; padding: 6px 10px; margin: 4px 0 8px;
        border-radius: 8px; font-size: .75rem; font-weight: 650; line-height: 1.5;
    }
    .badge-ok { color: #146647; background: #eaf6f0; border: 1px solid #b9ddcc; }
    .badge-warning { color: #70500b; background: #fff5d8; border: 1px solid #edcf82; }
    .badge-error { color: #9b2934; background: #fff0f1; border: 1px solid #efc6cb; }
    .badge-info { color: var(--navy); background: #edf4fc; border: 1px solid #c9ddf4; }
    .service-label { font-size: .77rem; color: #526783; margin: 8px 0 0; }
    .session-ref { font-size: .75rem; color: #60728a; overflow-wrap: anywhere; }
    .welcome { padding: 14px 0 18px; }
    .welcome h2 { font-size: 1.5rem; margin-bottom: .35rem; }
    .welcome p { color: #526783; max-width: 750px; }
    .capability {
        background: #fff; border: 1px solid #dce4ef; border-radius: 12px;
        padding: 18px; min-height: 134px; margin-bottom: 20px;
    }
    .capability strong { display: block; color: var(--navy); margin-bottom: 7px; }
    .capability span { font-size: .84rem; color: #526783; }
    .dictamen {
        color: var(--navy); font-size: .73rem; font-weight: 750; letter-spacing: .08em;
        text-transform: uppercase; padding-bottom: 9px; margin-bottom: 13px;
        border-bottom: 2px solid var(--gold);
    }
    [data-testid="stChatMessage"] {
        background: #fff; border: 1px solid #dce4ef; border-radius: 14px;
        padding: 20px; margin-bottom: 14px;
    }
    [data-testid="stChatInput"] { border-color: #c1d2e6; }
    [data-testid="stExpander"] { border-color: #dce4ef; border-radius: 9px; }
    .stButton > button { border-radius: 9px; font-size: .82rem; min-height: 44px; }
    .stButton > button:hover { color: var(--navy); border-color: var(--blue); background: #edf4fc; }
    button:focus-visible { outline: 3px solid var(--gold) !important; }
    [data-testid="stBottomBlockContainer"] { background: var(--bg); }
    @media (max-width: 640px) {
        .block-container { padding: 1rem 1rem 5rem; }
        .hero { padding: 22px 19px; }
        .hero h1 { font-size: 1.25rem; }
        [data-testid="stChatMessage"] { padding: 13px; }
    }
</style>
"""


def new_conversation() -> None:
    """El UUID nunca se reutiliza entre sesiones, perfiles ni clientes."""
    st.session_state.thread_id = f"banamex_web_{uuid4().hex}"
    st.session_state.messages = []


def initialize_session() -> None:
    if "thread_id" not in st.session_state:
        new_conversation()
    st.session_state.setdefault("active_identity", ("universal", None))


@st.cache_data(ttl=60, show_spinner=False)
def service_statuses() -> dict:
    """Solo se comparten comprobaciones de conexión; nunca mensajes ni evidencias."""
    return get_service_statuses()


def badge(text: str, kind: str = "info") -> None:
    kind = kind if kind in {"ok", "warning", "error", "info"} else "info"
    st.markdown(f'<div class="badge badge-{kind}">{escape(text)}</div>', unsafe_allow_html=True)


def render_sidebar() -> tuple[str, int | None]:
    with st.sidebar:
        st.markdown('<div class="brand">banamex<span class="brand-dot">.</span></div>', unsafe_allow_html=True)
        st.markdown('<div class="eyebrow">DATA STEWARD AI · GOBERNANZA</div>', unsafe_allow_html=True)
        st.divider()
        st.subheader("Configuración de sesión")
        profile = st.radio("Perfil de usuario", PROFILES, key="selected_profile")
        user_role, cliente_id = "universal", None
        if profile == PROFILES[1]:
            user_role = "cliente"
            cliente_id = int(st.number_input("Identificador de cliente · num_cte", min_value=1,
                                            value=2557, step=1, key="client_number"))
            badge("Row-Level Security Activo (Acceso exclusivo a su propio num_cte)", "warning")
            st.caption("Restricción aplicada por el validador SQL del backend.")
        else:
            badge("Acceso Total a SQL y Normativa")
        st.caption("Selector de perfil de demostración. No verifica la identidad ni la titularidad del num_cte.")

        identity = (user_role, cliente_id)
        if identity != st.session_state.active_identity:
            new_conversation()
            st.session_state.active_identity = identity

        st.divider()
        st.button("Nueva Conversación", icon="➕", key="new_conversation", on_click=new_conversation,
                  width="stretch", type="primary")
        st.markdown(f'<div class="session-ref">Sesión · {escape(st.session_state.thread_id[-12:])}</div>',
                    unsafe_allow_html=True)
        st.divider()
        st.subheader("Estado de servicios")
        if st.button("Verificar conexiones", key="refresh_services", width="stretch"):
            service_statuses.clear()
        try:
            statuses = service_statuses()
        except Exception:
            statuses = {}
        for service in ("PostgreSQL", "Pinecone", "LangSmith"):
            status = statuses.get(service, {"state": "warning", "label": "Sin verificar",
                                            "detail": "No se pudo comprobar el servicio."})
            st.markdown(f'<div class="service-label">{service}</div>', unsafe_allow_html=True)
            badge(str(status["label"]), str(status["state"]))
            st.caption(status["detail"])
        st.caption("Comprobaciones de solo lectura · actualización cada 60 segundos.")
    return user_role, cliente_id


def render_header(user_role: str, cliente_id: int | None) -> None:
    st.markdown('''<div class="hero">
        <div class="eyebrow">PLATAFORMA MULTI-AGENTE · HUB-AND-SPOKE</div>
        <h1>Grupo Financiero Banamex | Oficialía de Gobernanza de Datos (Data Steward AI)</h1>
        <p>Información financiera y normativa, con evidencia en cada respuesta.</p>
    </div>''', unsafe_allow_html=True)
    label = "Data Steward / Auditor" if user_role == "universal" else f"Cliente Banamex · num_cte {cliente_id}"
    badge(f"Sesión: {label}")


def render_welcome() -> None:
    st.markdown('''<div class="welcome"><h2>¿Qué necesitas analizar hoy?</h2>
        <p>Consulta datos del banco, revisa condiciones de productos y convierte tus resultados
        en gráficas. El coordinador consulta las fuentes disponibles y reúne el respaldo de su dictamen.</p>
    </div>''', unsafe_allow_html=True)
    cards = (
        ("📊 Análisis financiero", "Saldos, cuentas y movimientos disponibles en PostgreSQL."),
        ("📚 Normativa documentada", "Contratos, comisiones y requisitos del corpus de Pinecone."),
        ("🔍 Trazabilidad visible", "Consultas SQL, fuentes documentales y recorrido del Hub."),
    )
    for column, (title, description) in zip(st.columns(3), cards):
        with column:
            st.markdown(f'<div class="capability"><strong>{title}</strong><span>{description}</span></div>',
                        unsafe_allow_html=True)


def quick_suggestions(user_role: str, cliente_id: int | None) -> str | None:
    scope = f"exclusivamente del cliente num_cte = {cliente_id}" if user_role == "cliente" else "del banco"
    suggestions = (
        ("📊 Gráfica de Crecimiento de Ahorro y Cheques",
         f"Genera una gráfica del crecimiento mensual de los saldos de Ahorro y Cheques {scope}. "
         "Consulta los datos reales disponibles, ordena por mes y muestra la fecha de corte."),
        ("💳 Exenciones en Cuenta Priority (RAG)",
         "¿Cuáles son las condiciones para exentar comisiones en Cuenta Priority Banamex? "
         "Consulta la normativa y cita documento, página y vigencia cuando estén disponibles."),
        ("💰 Consultar mis saldos actuales",
         f"Consulta los saldos por cuenta y producto {scope} en el último corte disponible. "
         "Indica la fecha del corte y la moneda; no supongas que son saldos en tiempo real."),
    )
    chosen = None
    for index, (column, (label, prompt)) in enumerate(zip(st.columns(3), suggestions)):
        if column.button(label, key=f"suggestion_{index}", width="stretch"):
            chosen = prompt
    return chosen


def evidence_records(result: dict, node: str) -> list[dict]:
    return [record.get("data", {}) for record in result.get("evidences", []) if record.get("node") == node]


def render_sql_evidence(result: dict) -> None:
    records = evidence_records(result, "sql_agent")
    if not records:
        st.info("No se realizó una consulta relacional en este turno.")
    for index, evidence in enumerate(records, 1):
        st.markdown(f"**Consulta {index}**")
        error = evidence.get("error")
        if error == "ACCESS_DENIED":
            st.warning("Acceso denegado por la política de permisos (RBAC). La consulta no fue ejecutada.")
        elif error:
            diagnostic = public_service_error((evidence.get("diagnostic") or {}).get("code") or error, source="sql")
            st.error(f"{diagnostic['message']} Código: {diagnostic['code']}.")
        if evidence.get("sql_query"):
            st.code(str(evidence["sql_query"]), language="sql")
        raw = evidence.get("raw_result")
        if raw is None:
            continue
        rows = decode_sql_result(raw)
        if rows:
            st.dataframe(rows, width="stretch", hide_index=True)
            st.caption("Datos devueltos por el backend. Si no entrega encabezados, las columnas se numeran por posición.")
        elif rows == []:
            st.info("La consulta no devolvió filas.")
        else:
            st.caption("Resultado crudo; se conserva su formato original.")
            st.code(str(raw), language="text", wrap_lines=True)


def reference_url(value: object) -> str | None:
    """Solo URLs web; no convertir metadatos arbitrarios en enlaces ejecutables."""
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
        return url if parts.scheme in {"http", "https"} and parts.netloc else None
    except ValueError:
        return None


def render_rag_evidence(result: dict) -> None:
    records = evidence_records(result, "rag_agent")
    if not records:
        st.info("No se consultó el corpus documental en este turno.")
    for index, evidence in enumerate(records, 1):
        if len(records) > 1:
            st.markdown(f"**Recuperación {index}**")
        if evidence.get("error"):
            diagnostic = public_service_error((evidence.get("diagnostic") or {}).get("code") or evidence["error"], source="rag")
            st.error(f"{diagnostic['message']} Código: {diagnostic['code']}.")
        sources = evidence.get("retrieved_sources") or []
        if not sources:
            st.caption("El backend no devolvió referencias documentales para esta consulta.")
        for source in sources:
            if not isinstance(source, dict):
                continue
            st.text(str(source.get("source") or "Documento sin nombre"))
            page = source.get("page")
            product = source.get("producto") or "Sin clasificar"
            st.caption(f"Página: {page if page is not None else 'no informada'} · Producto: {product}")
            url = reference_url(source.get("url"))
            if url:
                st.link_button("Abrir referencia documental ↗", url)
            else:
                st.caption("URL de referencia no disponible.")


def render_orchestration(result: dict) -> None:
    steps = result.get("orchestration") or []
    if not steps:
        st.info("No se registraron eventos del coordinador.")
        return
    st.caption("Recorrido de la consulta")
    st.write(" → ".join(NODE_LABELS.get(step.get("node"), step.get("node", "Nodo")) for step in steps))
    for index, step in enumerate(steps, 1):
        if step.get("node") != "coordinator":
            continue
        destination = NODE_LABELS.get(step.get("next_agent"), step.get("next_agent") or "Sin destino")
        st.markdown(f"**Paso {index} · Hub → {destination}**")
        st.write(step.get("plan") or "Plan no informado por el backend.")
        if step.get("instruction"):
            st.caption("Instrucción delegada")
            st.code(str(step["instruction"]), language="text", wrap_lines=True)


def render_assistant(message: dict) -> None:
    result = message.get("result") or {}
    with st.chat_message("assistant", avatar="🏛️"):
        st.markdown('<div class="dictamen">Banamex · Dictamen oficial del Data Steward AI</div>',
                    unsafe_allow_html=True)
        if result.get("error"):
            st.error(result["error"])
        if message.get("content"):
            st.markdown(message["content"])
        chart_path = result.get("chart_path")
        if chart_path:
            path = resolve_chart_path(chart_path)
            if path:
                try:
                    st.image(str(path), use_container_width=True)
                except Exception:
                    st.warning("La gráfica no pudo mostrarse. Las evidencias siguen disponibles abajo.")
            else:
                st.warning("El archivo PNG de la gráfica ya no está disponible o su ruta no es válida.")
        with st.expander("🔍 Evidencias de Gobernanza y Auditoría"):
            sql_tab, rag_tab, hub_tab = st.tabs(["SQL / Base de Datos", "RAG / Normativa", "Orquestación Hub"])
            with sql_tab:
                render_sql_evidence(result)
            with rag_tab:
                render_rag_evidence(result)
            with hub_tab:
                render_orchestration(result)
        if message.get("created_at"):
            st.caption(f"Emitido · {message['created_at']} · Evidencias de este turno")


def process_question(question: str, user_role: str, cliente_id: int | None) -> None:
    question = question.strip()
    if not question:
        return
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.status("El coordinador está analizando tu consulta…", expanded=True) as status:
        def on_step(event: dict) -> None:
            node = event.get("node", "coordinator")
            status.write(f"✓ {NODE_LABELS.get(node, node)}")

        try:
            result = run_data_steward(question, st.session_state.thread_id, user_role, cliente_id,
                                     on_step=on_step)
        except Exception:
            result = {"error": "No fue posible completar la consulta. Revisa la configuración del backend e inténtalo de nuevo.",
                      "final_answer": None, "chart_path": None, "evidences": [], "orchestration": []}
        source_failed = any(record.get("data", {}).get("error") for record in result.get("evidences", []))
        label = "Consulta no completada" if result.get("error") else ("Consulta finalizada con incidencias" if source_failed else "Dictamen listo")
        status.update(label=label, state="error" if result.get("error") or source_failed else "complete", expanded=False)
    message = {"role": "assistant", "content": result.get("final_answer"), "result": result,
               "created_at": datetime.now(ZoneInfo("America/Mexico_City")).strftime("%d/%m/%Y %H:%M %Z")}
    st.session_state.messages.append(message)
    render_assistant(message)


def main() -> None:
    st.set_page_config(page_title="Banamex | Data Steward AI", page_icon="🏛️", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    initialize_session()
    user_role, cliente_id = render_sidebar()
    render_header(user_role, cliente_id)
    if not st.session_state.messages:
        render_welcome()
    st.caption("CONSULTAS SUGERIDAS")
    suggested_question = quick_suggestions(user_role, cliente_id)
    st.divider()
    for message in st.session_state.messages:
        if message["role"] == "user":
            with st.chat_message("user"):
                st.markdown(message["content"])
        else:
            render_assistant(message)
    question = st.chat_input("Escribe tu consulta financiera o normativa...", max_chars=8000,
                             key="question_input")
    if question or suggested_question:
        process_question(question or suggested_question, user_role, cliente_id)


if __name__ == "__main__":
    main()
