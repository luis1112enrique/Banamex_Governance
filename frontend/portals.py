"""Focused conversational views for institutional and personal banking."""

import streamlit as st

from frontend.backend_adapter import ask_assistant, validate_chart_path


_SUGGESTIONS = {
    "data_steward": (
        "Resume la cartera de ejemplo por segmento.",
        "¿Cómo se relacionan los análisis SQL y documental?",
        "Explica la evolución de los saldos sintéticos.",
    ),
    "auditor": (
        "¿Qué controles debemos revisar antes de habilitar datos reales?",
        "¿Qué evidencias y fuentes están disponibles en esta demostración?",
        "Explica la separación de permisos entre perfiles.",
    ),
    "cliente": (
        "¿Cuál es mi saldo de ejemplo?",
        "Muéstrame mis movimientos sintéticos.",
        "¿Qué productos puedo explorar en esta demostración?",
    ),
}

_REAL_SUGGESTIONS = {
    "data_steward": (
        "Resume los saldos disponibles por segmento.",
        "¿Qué documentación autorizada describe las comisiones?",
        "Grafica los saldos por segmento y explica las diferencias.",
    ),
    "auditor": (
        "¿Qué evidencias respaldan la respuesta anterior?",
        "¿Qué controles de acceso están habilitados?",
        "Revisa las fuentes disponibles para esta conversación.",
    ),
    "cliente": (
        "¿Cuál es mi saldo?",
        "Muéstrame mis últimos movimientos.",
        "¿Qué información hay disponible sobre productos?",
    ),
}


def _institutional_overview(profile: str) -> None:
    st.markdown(
        '<div class="hero institutional"><span class="eyebrow">'
        'PORTAL INSTITUCIONAL</span>'
        '<h1>Centro de Gobernanza y Análisis de Datos</h1>'
        '<p>Información financiera, normativa y análisis comparativos en una conversación.</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def _client_overview() -> None:
    st.markdown(
        '<div class="hero client"><span class="eyebrow">'
        'PORTAL CLIENTE</span>'
        '<h1>Tu banca, más clara.</h1>'
        '<p>Explora saldos, movimientos y productos con tu asistente de banca personal.</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def _render_message(message: dict) -> None:
    with st.chat_message(message["role"]):
        if message["content"]:
            st.markdown(message["content"])
        if message.get("error"):
            st.warning(message["error"])
        if message.get("chart_path"):
            chart_path = validate_chart_path(message["chart_path"])
            if chart_path:
                try:
                    st.image(chart_path, caption="Visualización de la respuesta")
                except Exception:
                    st.warning("La visualización no está disponible. Puedes continuar la conversación.")
            else:
                st.warning("La visualización recibida no es un PNG local válido.")


def render_portal(
    *, portal: str, profile: str, identity: str, thread_id: str, history: list[dict],
    demo_mode: bool = True, access_token: str | None = None,
    access_authorized: bool = False,
) -> None:
    """Render a portal; state and authorization are provided by the entry point."""
    if portal == "institucional":
        _institutional_overview(profile)
    else:
        _client_overview()

    profile_label = {"data_steward": "Data Steward", "auditor": "Auditor", "cliente": "Cliente"}[profile]
    st.caption(
        f"{profile_label} · {identity} · Sesión de pruebas verificada · Datos sintéticos" if demo_mode
        else f"{profile_label} · {identity} · Identidad de pruebas autorizada" if access_authorized
        else "Consulta real pendiente de habilitación · Puedes consultar el estado o usar Demostración."
    )
    if not demo_mode and access_authorized and profile != "data_steward":
        st.warning(
            "Tu identidad está verificada, pero las consultas reales de este perfil siguen deshabilitadas. "
            "Falta comprobar el aislamiento y los permisos de acceso a la información. "
            "Puedes cerrar sesión o usar la modalidad Demostración."
        )
    with st.container(key="conversation"):
        st.subheader("Asistente de revisión" if profile == "auditor" else "Tu asistente")
        st.caption(
            "Modo demostración · respuestas sintéticas."
            if demo_mode else "Las respuestas reales utilizan exclusivamente las fuentes de prueba habilitadas."
        )
        suggestion = None
        suggestions = (_SUGGESTIONS if demo_mode else _REAL_SUGGESTIONS)[profile]
        for index, column in enumerate(st.columns(3)):
            with column:
                if st.button(suggestions[index], key=f"banamex_prompt_{thread_id}_{index}", width="stretch"):
                    suggestion = suggestions[index]

        messages = st.container()
        with messages:
            if not history:
                st.info("Elige una pregunta sugerida o escribe la tuya para comenzar.")
            for message in history:
                _render_message(message)

        with st.container(key="chat_composer"):
            question = st.chat_input(
                "Escribe tu pregunta…", key=f"banamex_chat_{thread_id}", max_chars=4000,
            ) or suggestion
        if question and question.strip():
            user_message = {"role": "user", "content": question.strip()}
            history.append(user_message)
            with messages:
                _render_message(user_message)
                with st.spinner("Preparando respuesta…" if demo_mode else "Consultando información autorizada…"):
                    try:
                        response = ask_assistant(
                            question.strip(), thread_id=thread_id, portal=portal, profile=profile,
                            identity=identity, demo_mode=demo_mode, access_token=access_token,
                        )
                        assistant_message = {
                            "role": "assistant", "content": response.final_answer,
                            "chart_path": response.chart_path, "error": response.error,
                        }
                    except Exception:
                        assistant_message = {
                            "role": "assistant", "content": "",
                            "error": "No fue posible completar la consulta. Inténtalo nuevamente.",
                        }
                history.append(assistant_message)
                _render_message(assistant_message)

    if portal == "institucional":
        with st.expander("Evidencias y fuentes disponibles"):
            st.caption("El contrato actual entrega respuesta y gráfica; no expone SQL, fuentes RAG estructuradas ni identificadores de trazabilidad.")
            st.info("Las citas incluidas por el asistente deben revisarse en la respuesta. No se agregan evidencias ni trazas simuladas.")
    elif demo_mode:
        with st.expander("Resumen ilustrativo · datos sintéticos"):
            st.markdown(
                '<div class="summary-card"><span>Saldo ficticio · Cuenta Demo</span>'
                '<strong>$18,450.00 MXN</strong><span>Sin vinculación con una cuenta bancaria real.</span></div>',
                unsafe_allow_html=True,
            )
            st.caption("Serie sintética fija para demostrar una visualización.")
            st.bar_chart(
                {"Mes": ["Enero", "Febrero", "Marzo"], "Saldo ficticio (MXN)": [15200, 17100, 18450]},
                x="Mes", y="Saldo ficticio (MXN)", color="#0076CE",
            )
