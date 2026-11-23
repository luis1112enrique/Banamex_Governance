"""UI regression tests; all external services and model calls are mocked."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import streamlit as st
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
CLIENT_PROFILE = "Cliente Banamex"
UNIVERSAL_PROFILE = "Usuario Universal (Data Steward / Auditor)"
STATUSES = {
    "PostgreSQL": {"state": "ok", "label": "Activo", "detail": "Conexión verificada."},
    "Pinecone": {"state": "ok", "label": "Conectado", "detail": "Índice verificado."},
    "LangSmith": {"state": "warning", "label": "Desactivado", "detail": "Trazas desactivadas."},
}
RESULT = {
    "final_answer": "El saldo del último corte disponible es de 1200 MXN.",
    "chart_path": None,
    "evidences": [
        {"node": "sql_agent", "name": "sql_evidence", "data": {
            "sql_query": "SELECT num_cte, saldo_mes FROM saldos WHERE num_cte = 2557",
            "raw_result": "[(2557, 1200.0)]", "summary": "Resultado SQL", "error": None,
        }},
        {"node": "rag_agent", "name": "rag_evidence", "data": {
            "summary": "Normativa consultada", "error": None,
            "retrieved_sources": [{"source": "Cuenta_Priority.pdf", "page": 4,
                                   "producto": "Priority", "url": "https://www.banamex.com/priority"}],
        }},
    ],
    "orchestration": [
        {"node": "coordinator", "plan": "Consultar los saldos permitidos.", "next_agent": "sql_agent",
         "instruction": "Consultar únicamente num_cte = 2557", "step_count": 1},
        {"node": "sql_agent", "plan": None, "next_agent": None, "instruction": None, "step_count": None},
        {"node": "coordinator", "plan": "Emitir dictamen respaldado.", "next_agent": "FINISH",
         "instruction": None, "step_count": 1},
    ],
}


class StreamlitAppTests(unittest.TestCase):
    def setUp(self):
        st.cache_data.clear()
        health_patch = patch("ui_backend.get_service_statuses", return_value=deepcopy(STATUSES))
        backend_patch = patch("ui_backend.run_data_steward", side_effect=self.backend_response)
        self.health = health_patch.start()
        self.backend = backend_patch.start()
        self.addCleanup(health_patch.stop)
        self.addCleanup(backend_patch.stop)
        self.addCleanup(st.cache_data.clear)

    @staticmethod
    def backend_response(*args, **kwargs):
        callback = kwargs.get("on_step")
        if callback:
            for step in RESULT["orchestration"]:
                callback(deepcopy(step))
        return deepcopy(RESULT)

    def app(self):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        self.assert_clean(app)
        return app

    def assert_clean(self, app):
        self.assertEqual([element.message for element in app.exception], [])

    def submit(self, app, question="¿Cuáles son mis saldos?"):
        app.chat_input(key="question_input").set_value(question).run()
        self.assert_clean(app)
        return app

    @staticmethod
    def visible_text(app):
        return "\n".join(str(element.value) for kind in ("markdown", "text", "caption", "error", "warning")
                         for element in app.get(kind))

    def test_initial_render_and_service_badges(self):
        app = self.app()
        self.assertEqual(app.radio(key="selected_profile").value, UNIVERSAL_PROFILE)
        self.assertEqual(app.session_state["messages"], [])
        self.assertTrue(app.session_state["thread_id"].startswith("banamex_web_"))
        text = self.visible_text(app)
        self.assertIn("Grupo Financiero Banamex | Oficialía de Gobernanza", text)
        self.assertIn("Acceso Total a SQL y Normativa", text)
        self.assertIn("Activo", text)
        self.assertIn("Conectado", text)
        self.assertIn("Desactivado", text)
        self.assertEqual(len(app.chat_input), 1)
        self.backend.assert_not_called()

    def test_chat_persists_without_reexecuting_on_rerun(self):
        app = self.app()
        thread = app.session_state["thread_id"]
        self.submit(app)
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual([m["role"] for m in app.session_state["messages"]], ["user", "assistant"])
        self.assertIn(RESULT["final_answer"], self.visible_text(app))
        self.backend.assert_called_once()
        self.assertEqual(self.backend.call_args.args[:4], ("¿Cuáles son mis saldos?", thread, "universal", None))
        app.run()
        self.assert_clean(app)
        self.assertEqual(len(app.chat_message), 2)
        self.assertEqual(app.session_state["thread_id"], thread)
        self.backend.assert_called_once()

    def test_client_default_and_arguments_reach_backend(self):
        app = self.app()
        universal_thread = app.session_state["thread_id"]
        app.radio(key="selected_profile").set_value(CLIENT_PROFILE).run()
        self.assert_clean(app)
        self.assertEqual(app.number_input(key="client_number").value, 2557)
        self.assertNotEqual(universal_thread, app.session_state["thread_id"])
        self.assertIn("Row-Level Security Activo", self.visible_text(app))
        self.submit(app)
        self.assertEqual(self.backend.call_args.args[2:4], ("cliente", 2557))

    def test_changing_role_clears_history_and_thread(self):
        app = self.app()
        self.submit(app)
        previous_thread = app.session_state["thread_id"]
        app.radio(key="selected_profile").set_value(CLIENT_PROFILE).run()
        self.assert_clean(app)
        self.assertEqual(app.session_state["messages"], [])
        self.assertEqual(len(app.chat_message), 0)
        self.assertNotEqual(app.session_state["thread_id"], previous_thread)
        self.backend.assert_called_once()

    def test_changing_client_id_clears_history_and_thread(self):
        app = self.app()
        app.radio(key="selected_profile").set_value(CLIENT_PROFILE).run()
        self.submit(app)
        previous_thread = app.session_state["thread_id"]
        app.number_input(key="client_number").set_value(42).run()
        self.assert_clean(app)
        self.assertEqual(app.session_state["messages"], [])
        self.assertNotEqual(app.session_state["thread_id"], previous_thread)
        self.submit(app, "Mis cuentas")
        self.assertEqual(self.backend.call_args.args[2:4], ("cliente", 42))

    def test_new_conversation_preserves_profile_and_changes_thread(self):
        app = self.app()
        app.radio(key="selected_profile").set_value(CLIENT_PROFILE).run()
        self.submit(app)
        previous_thread = app.session_state["thread_id"]
        app.button(key="new_conversation").click().run()
        self.assert_clean(app)
        self.assertEqual(app.session_state["messages"], [])
        self.assertNotEqual(app.session_state["thread_id"], previous_thread)
        self.assertEqual(app.radio(key="selected_profile").value, CLIENT_PROFILE)
        self.assertEqual(app.number_input(key="client_number").value, 2557)
        self.backend.assert_called_once()

    def test_each_quick_suggestion_runs_once(self):
        for index, expected_text in enumerate(("crecimiento mensual", "Cuenta Priority", "último corte")):
            with self.subTest(index=index):
                self.backend.reset_mock()
                app = self.app()
                app.button(key=f"suggestion_{index}").click().run()
                self.assert_clean(app)
                self.backend.assert_called_once()
                self.assertIn(expected_text, self.backend.call_args.args[0])
                self.assertEqual(len(app.session_state["messages"]), 2)
                app.run()
                self.assert_clean(app)
                self.backend.assert_called_once()

    def test_client_savings_suggestion_includes_only_current_customer(self):
        app = self.app()
        app.radio(key="selected_profile").set_value(CLIENT_PROFILE).run()
        app.button(key="suggestion_0").click().run()
        self.assert_clean(app)
        self.assertIn("exclusivamente del cliente num_cte = 2557", self.backend.call_args.args[0])

    def test_sql_rag_and_hub_audit_render_together(self):
        app = self.submit(self.app())
        self.assertEqual([tab.label for tab in app.tabs],
                         ["SQL / Base de Datos", "RAG / Normativa", "Orquestación Hub"])
        self.assertEqual(app.expander[0].label, "🔍 Evidencias de Gobernanza y Auditoría")
        self.assertTrue(any("SELECT num_cte" in block.value for block in app.code))
        self.assertEqual(len(app.dataframe), 1)
        self.assertEqual(app.dataframe[0].value.shape, (1, 2))
        text = self.visible_text(app)
        self.assertIn("Cuenta_Priority.pdf", text)
        self.assertIn("Página: 4", text)
        self.assertIn("Consultar los saldos permitidos.", text)
        self.assertIn("Emitir dictamen respaldado.", text)
        self.assertEqual(len(app.get("link_button")), 1)

    def test_valid_png_chart_is_displayed_and_persists(self):
        charts_root = ROOT / "outputs" / "charts"
        charts_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ui_test_", dir=charts_root) as chart_dir:
            chart_path = Path(chart_dir) / "chart.png"
            Image.new("RGB", (16, 16), color="#002D72").save(chart_path)
            result = deepcopy(RESULT)
            result["chart_path"] = str(chart_path)
            self.backend.side_effect = None
            self.backend.return_value = result
            app = self.submit(self.app())
            self.assertEqual(len(app.get("image")), 1)
            app.run()
            self.assert_clean(app)
            self.assertEqual(len(app.get("image")), 1)
            self.backend.assert_called_once()

    def test_missing_chart_warns_instead_of_crashing(self):
        result = deepcopy(RESULT)
        result["chart_path"] = str(ROOT / "outputs" / "charts" / "missing-test-chart.png")
        self.backend.side_effect = None
        self.backend.return_value = result
        app = self.submit(self.app())
        self.assertIn("archivo PNG", self.visible_text(app))
        self.assertEqual(len(app.get("image")), 0)

    def test_backend_exception_uses_safe_error_and_keeps_chat_usable(self):
        self.backend.side_effect = RuntimeError("private-host password=do-not-display")
        app = self.submit(self.app())
        self.assertEqual(len(app.error), 1)
        self.assertIn("No fue posible completar", app.error[0].value)
        self.assertNotIn("do-not-display", self.visible_text(app))
        self.assertEqual(len(app.session_state["messages"]), 2)
        self.assertEqual(len(app.chat_input), 1)
        app.run()
        self.assert_clean(app)
        self.backend.assert_called_once()

    def test_sql_model_error_shows_actionable_diagnostic_not_access_warning(self):
        diagnostic = {
            "code": "MODEL_NOT_FOUND",
            "message": "Internal error: private-diagnostic-host api_key=not-for-ui",
        }
        result = deepcopy(RESULT)
        result["final_answer"] = "No se pudieron consultar los saldos en este momento."
        result["evidences"] = [{"node": "sql_agent", "name": "sql_evidence", "data": {
            "sql_query": None, "raw_result": None, "summary": "La fuente no pudo completar la consulta.",
            "error": "SOURCE_UNAVAILABLE", "diagnostic": diagnostic,
        }}]
        self.backend.side_effect = None
        self.backend.return_value = result
        app = self.submit(self.app())
        self.assertEqual(len(app.error), 1)
        self.assertIn(diagnostic["code"], app.error[0].value)
        self.assertIn("SQL_MODEL", app.error[0].value)
        self.assertNotIn("private-diagnostic-host", self.visible_text(app))
        self.assertNotIn("not-for-ui", self.visible_text(app))
        self.assertEqual(len(app.warning), 0)
        self.assertNotIn("fue bloqueada", self.visible_text(app))

    def test_sql_access_denied_is_a_distinct_permission_warning(self):
        result = deepcopy(RESULT)
        result["final_answer"] = "Tu perfil solo permite consultar tus propios datos."
        result["evidences"] = [{"node": "sql_agent", "name": "sql_evidence", "data": {
            "sql_query": "BLOQUEADA POR POLÍTICA RBAC (ACCESS_DENIED)", "raw_result": None,
            "summary": "Acceso denegado: tu perfil permite consultar únicamente tu propio num_cte.",
            "error": "ACCESS_DENIED",
        }}]
        self.backend.side_effect = None
        self.backend.return_value = result
        app = self.submit(self.app())
        self.assertEqual(len(app.error), 0)
        self.assertEqual(len(app.warning), 1)
        warning = app.warning[0].value.lower()
        self.assertIn("acceso", warning)
        self.assertTrue("denegado" in warning or "permisos" in warning)

    def test_rag_not_consulted_remains_informational(self):
        result = deepcopy(RESULT)
        result["evidences"] = [record for record in result["evidences"] if record["node"] != "rag_agent"]
        self.backend.side_effect = None
        self.backend.return_value = result
        app = self.submit(self.app())
        rag_tab = app.tabs[1]
        self.assertEqual(rag_tab.label, "RAG / Normativa")
        self.assertEqual(len(rag_tab.info), 1)
        self.assertIn("No se consultó el corpus documental en este turno", rag_tab.info[0].value)
        self.assertEqual(len(rag_tab.error), 0)
        self.assertEqual(len(rag_tab.warning), 0)

    def test_service_exception_shows_unverified_badges(self):
        self.health.side_effect = RuntimeError("private-service-error")
        app = self.app()
        self.assertIn("Sin verificar", self.visible_text(app))
        self.assertNotIn("private-service-error", self.visible_text(app))

    def test_health_refresh_does_not_repeat_chat_request(self):
        app = self.submit(self.app())
        self.assertEqual(self.health.call_count, 1)
        app.button(key="refresh_services").click().run()
        self.assert_clean(app)
        self.assertEqual(self.health.call_count, 2)
        self.backend.assert_called_once()

    def test_separate_web_sessions_have_different_threads(self):
        first = self.app()
        second = self.app()
        self.assertNotEqual(first.session_state["thread_id"], second.session_state["thread_id"])


if __name__ == "__main__":
    unittest.main()
