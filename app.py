"""Streamlit entry point for the institutional and personal banking portals."""

from uuid import uuid4

import streamlit as st

from frontend.portals import render_portal
from frontend.backend_adapter import (
    authenticate_test_user,
    backend_status,
    get_access_context,
    is_authenticated_login,
    revoke_access,
)


STYLES = """
<style>
:root { --navy: #002D72; --blue: #0076CE; --gold: #FFB81C; --light: #F4F6F9; color-scheme: light; }
.block-container { max-width: 1160px; padding-top: 1.5rem; padding-bottom: 2rem; }
.brand { display: flex; align-items: center; gap: .7rem; color: var(--navy);
    font-size: .85rem; font-weight: 800; letter-spacing: .09em; }
.brand-mark { width: 8px; height: 24px; background: var(--blue); border-radius: 3px;
    border-bottom: 5px solid var(--gold); }
.brand span { color: #536782; font-weight: 500; letter-spacing: .05em; }
.eyebrow { color: var(--blue); font-size: .7rem; font-weight: 750;
    letter-spacing: .1em; text-transform: uppercase; }
.demo-banner { padding: .65rem .9rem; background: #FFF9E9; color: #64500F;
    border: 1px solid #EBDCA8; border-radius: 9px; margin: 1rem 0 .7rem; font-size: .86rem; }
.hero { padding: .8rem 0 .7rem; margin: .3rem 0 .6rem; border-bottom: 1px solid #D9E2EF; }
.hero h1 { color: var(--navy); font-size: clamp(1.6rem, 2.7vw, 2rem); line-height: 1.25;
    letter-spacing: -.03em; margin: .4rem 0 .45rem; }
.hero p { color: #536782; margin: 0 0 .45rem; font-size: .94rem; }
.summary-card { border: 1px solid #D9E2EF; background: white; border-radius: 10px; padding: 1rem; }
.summary-card span { color: #536782; font-size: .8rem; display: block; }
.summary-card strong { color: var(--navy); font-size: clamp(1.05rem, 1.8vw, 1.4rem);
    display: block; line-height: 1.5; overflow-wrap: anywhere; }
.st-key-conversation { max-width: 940px; margin-inline: auto; }
.st-key-chat_composer { padding: .5rem 0 .1rem; }
.st-key-portal_navigation { max-width: 590px; margin-inline: auto; }
.st-key-login_panel { max-width: 530px; margin: 1rem auto 2rem; background: white; }
[data-testid="stChatMessage"] { border: 1px solid #D9E2EF; border-radius: 12px; }
[data-testid="stSidebar"] { border-right: 1px solid #D9E2EF; }
.stButton > button { border-radius: 8px; }
.stButton > button[kind="secondary"] { background: white; border-color: #93AFD2; color: var(--navy); }
.stButton > button[kind="primary"] { background: var(--blue); border-color: var(--blue); color: white; }
.stButton > button:focus-visible { outline: 3px solid var(--gold); outline-offset: 2px; }
.stButton > button[kind="secondary"]:hover { background: #EFF4FA; border-color: var(--blue); }
.stButton > button[kind="primary"]:hover { background: #0062AC; border-color: #0062AC; }
[data-testid="stChatInputSubmitButton"] { color: var(--blue); }
@media (max-width: 700px) {
    .block-container { padding-top: 1rem; }
    .hero { padding-top: .4rem; }
    .brand { font-size: .74rem; gap: .45rem; }
}
</style>
"""

PROFILE_LABELS = {"data_steward": "Data Steward", "auditor": "Auditor", "cliente": "Cliente"}


def _clear_conversation() -> None:
    """Discard every frontend artifact when identity or context changes."""
    for key in list(st.session_state):
        if key in {"banamex_context", "banamex_thread", "banamex_history"} or key.startswith(
            ("banamex_chat_", "banamex_prompt_")
        ):
            del st.session_state[key]


def _select_portal(portal: str | None) -> None:
    revoke_access(st.session_state.pop("banamex_access_token", None))
    _clear_conversation()
    for key in (
        "banamex_profile", "banamex_identity", "banamex_login_error",
        "banamex_login_user", "banamex_login_password", "banamex_access_error", "banamex_mode",
    ):
        st.session_state.pop(key, None)
    if portal in {"institucional", "cliente"} and st.session_state.get("banamex_portal") != portal:
        st.session_state["banamex_portal"] = portal


def _change_portal() -> None:
    portal = st.session_state.get("banamex_portal") or "institucional"
    st.session_state["banamex_portal"] = portal
    _select_portal(portal)


def _change_mode() -> None:
    _clear_conversation()


def _login(portal: str) -> None:
    result = authenticate_test_user(
        st.session_state.get("banamex_login_user", ""),
        st.session_state.get("banamex_login_password", ""),
        portal=portal,
    )
    st.session_state["banamex_login_password"] = ""
    st.session_state["banamex_login_error"] = result.error
    if result.access_token:
        revoke_access(st.session_state.pop("banamex_access_token", None))
        _clear_conversation()
        st.session_state["banamex_access_token"] = result.access_token
        st.session_state["banamex_mode"] = (
            "Backend real · pruebas" if result.profile == "data_steward" else "Demostración"
        )


def _logout() -> None:
    _select_portal(st.session_state.get("banamex_portal", "institucional"))


def _login_screen(portal: str) -> None:
    institutional = portal == "institucional"
    with st.container(key="login_panel", border=True):
        st.markdown(
            '<div class="eyebrow">Portal Institucional</div>' if institutional
            else '<div class="eyebrow">Portal Cliente</div>',
            unsafe_allow_html=True,
        )
        st.subheader("Acceso institucional" if institutional else "Acceso a tu banca")
        st.caption(
            "Ingresa con tu cuenta Data Steward o Auditor. El perfil se asigna según tu identidad."
            if institutional else "Ingresa con tu cuenta de cliente asignada para este entorno de pruebas."
        )
        with st.form("test_access_login"):
            st.text_input(
                "Usuario institucional" if institutional else "Usuario de cliente",
                key="banamex_login_user", placeholder="Usuario asignado", autocomplete="username",
            )
            st.text_input(
                "Contraseña", type="password", key="banamex_login_password",
                autocomplete="current-password",
            )
            st.form_submit_button(
                "Entrar al Portal Institucional" if institutional else "Entrar al Portal Cliente",
                type="primary", width="stretch", on_click=_login, args=(portal,),
            )
        if st.session_state.get("banamex_login_error"):
            st.error(st.session_state["banamex_login_error"])
        st.caption("Acceso de desarrollo y pruebas. Cada cuenta conserva su identidad y alcance.")


def main() -> None:
    st.set_page_config(
        page_title="Banamex | Data Steward AI", page_icon="🏦", layout="wide",
    )
    st.markdown(STYLES, unsafe_allow_html=True)
    st.markdown('<div class="brand"><i class="brand-mark"></i>BANAMEX <span>/ DATA STEWARD AI</span></div>', unsafe_allow_html=True)
    st.caption("Una plataforma. Dos formas de consultar.")
    if st.session_state.get("banamex_portal") not in {"institucional", "cliente"}:
        st.session_state["banamex_portal"] = "institucional"
    with st.container(key="portal_navigation"):
        portal = st.segmented_control(
            "Selecciona tu portal", ("institucional", "cliente"),
            format_func=lambda value: "Portal Institucional" if value == "institucional" else "Portal Cliente",
            key="banamex_portal", on_change=_change_portal, selection_mode="single",
        )
    if portal not in {"institucional", "cliente"}:
        portal = "institucional"
    access_token = st.session_state.get("banamex_access_token")
    principal = get_access_context(access_token) if is_authenticated_login(access_token) else None
    if principal is None or principal["portal"] != portal:
        if access_token:
            _logout()
        _login_screen(portal)
        return

    profile = principal["profile"]
    identity = principal["identity"]
    if "banamex_mode" not in st.session_state:
        st.session_state["banamex_mode"] = (
            "Backend real · pruebas" if profile == "data_steward" else "Demostración"
        )
    with st.sidebar:
        st.subheader("Mi sesión")
        st.write(identity)
        st.caption("Perfil: " + PROFILE_LABELS[profile])
        st.button("Cerrar sesión", key="banamex_logout", on_click=_logout, width="stretch")
        mode = st.radio(
            "Modalidad", ("Backend real · pruebas", "Demostración"),
            key="banamex_mode", on_change=_change_mode,
        )
        if profile != "data_steward":
            st.caption("Las consultas reales de este perfil permanecen restringidas. La demostración usa tu misma identidad y datos sintéticos.")
    demo_mode = mode == "Demostración"
    if demo_mode:
        st.markdown(
            '<div class="demo-banner"><strong>Modo demostración.</strong> Sesión autenticada; '
            'las cifras y respuestas son sintéticas y no consultan datos bancarios reales.</div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("Desarrollo local · consultas al backend sobre fuentes de prueba autorizadas.")

    with st.sidebar:
        if demo_mode:
            st.caption("Estado: simulación local, sin conexión a fuentes bancarias.")
        else:
            status = backend_status()
            st.caption(status["message"])
        st.button("Nueva conversación", on_click=_clear_conversation, width="stretch")
        st.caption("Cambiar de portal o cerrar sesión descarta el contexto anterior.")

    context = (portal, profile, identity, mode, access_token if not demo_mode else None)
    if st.session_state.get("banamex_context") != context:
        _clear_conversation()
        st.session_state["banamex_context"] = context
        st.session_state["banamex_thread"] = str(uuid4())
        st.session_state["banamex_history"] = []

    if portal == "institucional":
        st.caption("Sesión: " + st.session_state["banamex_thread"][:8])

    render_portal(
        portal=portal, profile=profile, identity=identity,
        thread_id=st.session_state["banamex_thread"], history=st.session_state["banamex_history"],
        demo_mode=demo_mode, access_token=access_token,
        access_authorized=True,
    )


if __name__ == "__main__":
    main()
