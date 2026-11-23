"""Adapter regression tests; every external service is replaced by a mock."""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from PIL import Image

import ui_backend as backend


class FakeGraph:
    def __init__(self, events=(), error=None):
        self.events = events
        self.error = error
        self.calls = []

    def stream(self, initial_input, *, config, stream_mode):
        self.calls.append((initial_input, config, stream_mode))
        for event in self.events:
            yield copy.deepcopy(event)
        if self.error:
            raise self.error


class RunTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.object(backend, "_load_environment")
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_graph(self, graph, **kwargs):
        return backend.run_data_steward(
            "Consulta financiera", "unique-thread", "cliente", 2557, graph=graph, **kwargs
        )

    def test_new_turn_resets_work_but_passes_thread_and_identity(self):
        graph = FakeGraph([{"coordinator": {"final_answer": "Dictamen"}}])
        self.run_graph(graph)
        self.run_graph(graph)
        for initial, config, stream_mode in graph.calls:
            self.assertEqual(initial["evidences"], {})
            self.assertEqual(initial["uploaded_files"], [])
            self.assertIsNone(initial["chart_path"])
            self.assertIsNone(initial["final_answer"])
            self.assertIsNone(initial["spoke_instruction"])
            self.assertEqual(initial["step_count"], 0)
            self.assertEqual(len(initial["messages"]), 1)
            self.assertEqual(initial["user_role"], "cliente")
            self.assertEqual(initial["cliente_id"], 2557)
            self.assertEqual(config["configurable"]["thread_id"], "unique-thread")
            self.assertEqual(stream_mode, "updates")
        self.assertIsNot(graph.calls[0][0]["evidences"], graph.calls[1][0]["evidences"])

    def test_preserves_repeated_sources_and_records_every_hop(self):
        events = [
            {"coordinator": {"plan": "Consultar", "next_agent": "sql_agent", "step_count": 1}},
            {"sql_agent": {"evidences": {"sql_evidence": {"sql_query": "SELECT 1", "raw_result": "[(1,)]"}}}},
            {"sql_agent": {"evidences": {"sql_evidence": {"sql_query": "SELECT 2", "raw_result": "[(2,)]"}}}},
            {"rag_agent": {"evidences": {"rag_evidence": {"retrieved_sources": [{"source": "norma.pdf", "page": 2}]}}}},
            {"coordinator": {"plan": "Sintetizar", "next_agent": "FINISH", "final_answer": "Dictamen"}},
        ]
        steps = []
        result = self.run_graph(FakeGraph(events), on_step=steps.append)
        self.assertEqual(result["final_answer"], "Dictamen")
        self.assertNotIn("error", result)
        self.assertEqual(len(result["evidences"]), 3)
        self.assertEqual(result["evidences"][1]["data"]["sql_query"], "SELECT 2")
        self.assertEqual(len(result["orchestration"]), 5)
        self.assertEqual(len(steps), 5)
        steps[0]["plan"] = "changed by caller"
        self.assertEqual(result["orchestration"][0]["plan"], "Consultar")

    def test_partial_failure_preserves_evidence_and_hides_exception(self):
        graph = FakeGraph(
            [{"sql_agent": {"evidences": {"sql_evidence": {"sql_query": "SELECT 1", "raw_result": "[(1,)]"}}}}],
            RuntimeError("private-credential-example"),
        )
        result = self.run_graph(graph)
        self.assertEqual(len(result["evidences"]), 1)
        self.assertEqual(result["error"], backend.RUN_ERROR)
        self.assertNotIn("private-credential-example", str(result))

    def test_source_exception_is_sanitized_but_access_denial_is_preserved(self):
        graph = FakeGraph([
            {"rag_agent": {"evidences": {"rag_evidence": {"summary": "credential-example", "error": "credential-example"}}}},
            {"sql_agent": {"evidences": {"sql_evidence": {"summary": "Acceso denegado", "error": "ACCESS_DENIED"}}}},
            {"coordinator": {"final_answer": "Limitación"}},
        ])
        result = self.run_graph(graph)
        self.assertNotIn("credential-example", str(result))
        self.assertEqual(result["evidences"][0]["data"]["error"], "SOURCE_UNAVAILABLE")
        self.assertEqual(result["evidences"][1]["data"]["summary"], "Acceso denegado")

    def test_source_model_error_keeps_actionable_diagnosis_without_provider_details(self):
        graph = FakeGraph([
            {"sql_agent": {"evidences": {"sql_evidence": {
                "sql_query": None, "raw_result": None,
                "summary": "404 NOT_FOUND private-model key=private-secret",
                "error": "404 NOT_FOUND private-model key=private-secret",
            }}}},
            {"coordinator": {"final_answer": "La consulta SQL no pudo completarse."}},
        ])
        result = self.run_graph(graph)
        evidence = result["evidences"][0]["data"]
        self.assertEqual(evidence["error"], "MODEL_NOT_FOUND")
        self.assertEqual(evidence["diagnostic"]["code"], "MODEL_NOT_FOUND")
        self.assertIn("SQL_MODEL", evidence["diagnostic"]["message"])
        self.assertNotIn("private-model", str(result))
        self.assertNotIn("private-secret", str(result))

    def test_invalid_identity_never_calls_graph(self):
        cases = [
            ("cliente", None), ("cliente", 0), ("cliente", -1),
            ("cliente", True), ("cliente", "2557"),
            ("universal", 2557), ("admin", None),
        ]
        for role, customer in cases:
            with self.subTest(role=role, customer=customer):
                graph = FakeGraph()
                result = backend.run_data_steward("pregunta", "thread", role, customer, graph=graph)
                self.assertIn("error", result)
                self.assertEqual(graph.calls, [])

    def test_missing_final_answer_is_reported(self):
        result = self.run_graph(FakeGraph([{"coordinator": {"plan": "Sin dictamen"}}]))
        self.assertIn("error", result)
        self.assertEqual(len(result["orchestration"]), 1)

    def test_keyboard_interrupt_is_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_graph(FakeGraph(error=KeyboardInterrupt()))


class DecodeTests(unittest.TestCase):
    def test_literal_rows_and_json_records(self):
        self.assertEqual(
            backend.decode_sql_result("[(1, 'A'), (2, 'B')]") ,
            [{"columna_1": 1, "columna_2": "A"}, {"columna_1": 2, "columna_2": "B"}],
        )
        self.assertEqual(backend.decode_sql_result('[{"saldo": 5}]'), [{"saldo": 5}])
        self.assertEqual(backend.decode_sql_result("[]"), [])
        self.assertEqual(backend.decode_sql_result([1, 2]), [{"valor": 1}, {"valor": 2}])

    def test_expressions_are_never_executed(self):
        with patch("os.system") as system:
            self.assertIsNone(backend.decode_sql_result("__import__('os').system('echo unsafe')"))
            system.assert_not_called()
        for raw in (
            "[(Decimal(__import__('os').system('echo unsafe')),)]",
            "[(datetime.__getattribute__('date')(2026, 1, 1),)]",
            "[(datetime.date.__class__(2026, 1, 1),)]",
            "[x for x in range(10)]",
            "[(lambda: 1)()]",
        ):
            self.assertIsNone(backend.decode_sql_result(raw))
        self.assertIsNone(backend.decode_sql_result("[" * 2000))
        self.assertIsNone(backend.decode_sql_result("x" * 1_000_001))

    def test_financial_and_date_repr_convert_to_display_values(self):
        raw = "[(Decimal('1234.50'), datetime.date(2026, 1, 31), datetime.datetime(2026, 1, 31, 10, 30, 1), datetime.time(10, 30), -1)]"
        self.assertEqual(backend.decode_sql_result(raw), [{
            "columna_1": "1234.50", "columna_2": "2026-01-31",
            "columna_3": "2026-01-31T10:30:01", "columna_4": "10:30:00", "columna_5": -1,
        }])
        self.assertIsNone(backend.decode_sql_result("[(datetime.date(2026, 13, 40),)]"))


class ChartPathTests(unittest.TestCase):
    def test_only_valid_pngs_within_chart_directory_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            charts = root / "outputs" / "charts"
            charts.mkdir(parents=True)
            valid = charts / "valid.png"
            Image.new("RGB", (10, 10)).save(valid)
            outside = root / "outside.png"
            Image.new("RGB", (10, 10)).save(outside)
            (charts / "link.png").symlink_to(outside)
            (charts / "bad.png").write_text("not a PNG")
            wrong_type = charts / "photo.png"
            Image.new("RGB", (10, 10)).save(wrong_type, format="JPEG")
            with patch.object(backend, "PROJECT_ROOT", root), patch.object(backend, "CHARTS_ROOT", charts):
                self.assertEqual(backend.resolve_chart_path("outputs/charts/valid.png"), valid)
                for path in (outside, charts / "link.png", charts / "bad.png", wrong_type, root / ".env", "missing.png", None):
                    with self.subTest(path=path):
                        self.assertIsNone(backend.resolve_chart_path(path))


class ServiceStatusTests(unittest.TestCase):
    def test_disabled_tracing_does_not_contact_langsmith(self):
        with patch.dict("os.environ", {"LANGSMITH_TRACING": "false"}, clear=True), patch("requests.get") as get:
            status = backend._langsmith_status()
            self.assertEqual(status["state"], "warning")
            self.assertIn("desactivada", status["label"])
            get.assert_not_called()

    def test_active_tracing_requires_authenticated_response(self):
        environment = {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": "test-placeholder"}
        response = Mock(status_code=200)
        response.json.return_value = {"id": "mock-workspace"}
        with patch.dict("os.environ", environment, clear=True), patch("requests.get", return_value=response) as get:
            status = backend._langsmith_status()
            self.assertEqual(status["state"], "ok")
            self.assertEqual(status["label"], "Trazabilidad activa")
            self.assertTrue(get.call_args.args[0].endswith("/settings"))
            self.assertEqual(get.call_args.kwargs["timeout"], (2, 3))
            response.status_code = 403
            self.assertEqual(backend._langsmith_status()["state"], "error")

    def test_missing_pinecone_key_and_rejected_key_are_distinct(self):
        with patch.dict("os.environ", {}, clear=True), patch("requests.get") as get:
            self.assertEqual(backend._pinecone_status()["state"], "warning")
            get.assert_not_called()
        response = Mock(status_code=401)
        with patch.dict("os.environ", {"PINECONE_API_KEY": "test-placeholder"}, clear=True), patch("requests.get", return_value=response):
            self.assertEqual(backend._pinecone_status()["state"], "error")

    def test_postgres_uses_read_only_probe_with_timeout(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (1,)
        connect = Mock(return_value=connection)
        with patch.dict("sys.modules", {"psycopg2": SimpleNamespace(connect=connect)}):
            status = backend._postgresql_status()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(connect.call_args.kwargs["connect_timeout"], 3)
        self.assertIn("default_transaction_read_only=on", connect.call_args.kwargs["options"])
        cursor.execute.assert_called_once_with("SELECT 1")
        connection.close.assert_called_once()

    def test_parallel_statuses_tolerate_one_failed_check(self):
        ok = {"state": "ok", "label": "mock", "detail": "mock"}
        with patch.object(backend, "_load_environment"), patch.object(backend, "_postgresql_status", return_value=ok), patch.object(backend, "_pinecone_status", side_effect=RuntimeError("credential-example")), patch.object(backend, "_langsmith_status", return_value=ok):
            statuses = backend.get_service_statuses()
        self.assertEqual(set(statuses), {"PostgreSQL", "Pinecone", "LangSmith"})
        self.assertEqual(statuses["Pinecone"]["state"], "warning")
        self.assertNotIn("credential-example", str(statuses))


if __name__ == "__main__":
    unittest.main()
