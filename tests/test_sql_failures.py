"""Failure diagnostics for SQL; no service calls or real credentials."""

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from agents import sql_agent
from service_errors import ERROR_CODES, public_service_error


class ModelError(Exception):
    status_code = 404


class SchemaError(Exception):
    pgcode = "42703"


class ServiceErrorTests(unittest.TestCase):
    def test_http_diagnostics_are_safe_and_actionable(self):
        cases = (
            ("404 NOT_FOUND models/private-model with key=private-secret", "MODEL_NOT_FOUND", "SQL_MODEL"),
            ("429 RESOURCE_EXHAUSTED quota exceeded key=private-secret", "QUOTA_EXCEEDED", "cuota"),
            ("401 UNAUTHENTICATED key=private-secret", "AUTHENTICATION_ERROR", "GOOGLE_API_KEY"),
            ("400 API_KEY_INVALID private-secret", "AUTHENTICATION_ERROR", "GOOGLE_API_KEY"),
            ("403 PERMISSION_DENIED private-secret", "PERMISSION_DENIED", "permisos"),
            ("403 Forbidden private-secret", "PERMISSION_DENIED", "permisos"),
            ("401 Unauthorized private-secret", "AUTHENTICATION_ERROR", "GOOGLE_API_KEY"),
            ("429 Too Many Requests private-secret", "QUOTA_EXCEEDED", "cuota"),
            ("connection refused password=private-secret", "DATABASE_CONNECTION_ERROR", "PGHOST"),
            ("opaque exception private-secret", "SOURCE_UNAVAILABLE", "configuración"),
        )
        for error, code, hint in cases:
            with self.subTest(code=code):
                diagnostic = public_service_error(error)
                self.assertEqual(diagnostic["code"], code)
                self.assertIn(hint, diagnostic["message"])
                self.assertNotIn("private-secret", str(diagnostic))
                self.assertNotIn("private-model", str(diagnostic))

    def test_structured_codes_and_wrapped_postgres_errors(self):
        error = Exception("secret-postgres-message")
        error.orig = SchemaError("secret-column")
        self.assertEqual(public_service_error(error)["code"], "SQL_SCHEMA_ERROR")
        error.orig.pgcode = "42P01"
        self.assertEqual(public_service_error(error)["code"], "SQL_SCHEMA_ERROR")
        error.orig.pgcode = "42601"
        self.assertEqual(public_service_error(error)["code"], "SQL_SYNTAX_ERROR")
        for status, code in ((401, "AUTHENTICATION_ERROR"), (403, "PERMISSION_DENIED"),
                             (404, "MODEL_NOT_FOUND"), (429, "QUOTA_EXCEEDED")):
            error = Exception("secret-status-message")
            error.response = SimpleNamespace(status_code=status)
            self.assertEqual(public_service_error(error)["code"], code)

    def test_canonical_codes_retain_diagnosis_and_source_hint(self):
        for code in ERROR_CODES:
            self.assertEqual(public_service_error(code)["code"], code)
        self.assertIn("RAG_MODEL", public_service_error("MODEL_NOT_FOUND", "rag")["message"])
        self.assertIn("el modelo configurado", public_service_error("MODEL_NOT_FOUND", "backend")["message"])
        self.assertNotIn("SQL_MODEL", public_service_error("MODEL_NOT_FOUND", "backend")["message"])

    def test_ordinary_ids_are_not_mistaken_for_http_statuses(self):
        diagnostic = public_service_error("Unknown failure for num_cte = 404; account 401")
        self.assertEqual(diagnostic["code"], "SOURCE_UNAVAILABLE")


class SQLFailureTests(unittest.TestCase):
    def setUp(self):
        self.llm = MagicMock()
        self.db = MagicMock()
        self.db.run.return_value = "[(1,)]"
        self.llm.invoke.return_value = SimpleNamespace(content="```sql\nSELECT 1;\n```")
        llm_patch = patch.object(sql_agent, "get_sql_llm", return_value=self.llm)
        db_patch = patch.object(sql_agent, "get_db_instance", return_value=self.db)
        llm_patch.start()
        db_patch.start()
        self.addCleanup(llm_patch.stop)
        self.addCleanup(db_patch.stop)

    def test_model_failure_returns_safe_diagnostic_without_executing_sql(self):
        self.llm.invoke.side_effect = ModelError("model/private-model?key=private-secret")
        result = sql_agent.run_sql_agent("Saldos mensuales")
        self.assertEqual(result["error"], "MODEL_NOT_FOUND")
        self.assertIn("SQL_MODEL", result["diagnostic"]["message"])
        self.assertIsNone(result["sql_query"])
        self.assertNotIn("private-secret", str(result))
        self.assertNotIn("private-model", str(result))
        self.db.run.assert_not_called()
        self.db._engine.dispose.assert_called_once()

    def test_execution_error_preserves_generated_query_and_safe_schema_diagnostic(self):
        self.db.run.side_effect = SchemaError("column missing; postgres://private-host/private-secret")
        result = sql_agent.run_sql_agent("Saldos mensuales")
        self.assertEqual(result["sql_query"], "SELECT 1;")
        self.assertEqual(result["error"], "SQL_SCHEMA_ERROR")
        self.assertEqual(result["summary"], result["diagnostic"]["message"])
        self.assertIsNone(result["raw_result"])
        self.assertNotIn("private-host", str(result))
        self.assertNotIn("private-secret", str(result))
        self.db.run.assert_called_once_with("SELECT 1;")
        self.db._engine.dispose.assert_called_once()

    def test_model_access_denied_preserves_existing_rbac_response(self):
        self.llm.invoke.return_value = SimpleNamespace(content="ACCESS_DENIED")
        result = sql_agent.run_sql_agent("Datos de otro cliente", "cliente", 2557)
        self.assertEqual(result["error"], "ACCESS_DENIED")
        self.assertIn("BLOQUEADA", result["sql_query"])
        self.assertNotIn("diagnostic", result)
        self.db.run.assert_not_called()

    def test_validator_access_denied_still_prevents_execution(self):
        self.llm.invoke.return_value = SimpleNamespace(content="SELECT * FROM dim.dim_cliente;")
        result = sql_agent.run_sql_agent("Datos de todos los clientes", "cliente", 2557)
        self.assertEqual(result["error"], "ACCESS_DENIED")
        self.assertIn("BLOQUEADA POR VALIDADOR", result["sql_query"])
        self.db.run.assert_not_called()

    def test_success_contract_is_unchanged(self):
        result = sql_agent.run_sql_agent("Una consulta simple")
        self.assertIsNone(result["error"])
        self.assertEqual(result["sql_query"], "SELECT 1;")
        self.assertEqual(result["raw_result"], "[(1,)]")
        self.assertNotIn("diagnostic", result)


if __name__ == "__main__":
    unittest.main()
