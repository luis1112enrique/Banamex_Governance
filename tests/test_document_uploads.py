"""Exercise the HTTP/file boundary without a database, OCR, or LLM."""

from contextvars import ContextVar
from pathlib import Path
import secrets
from types import ModuleType, SimpleNamespace
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import authorization as auth
from api.main import app
from api.services import assistant, uploads


class DocumentUploadTests(unittest.TestCase):
    def setUp(self):
        self.sources = {"postgres_host": "fixture", "postgres_port": "5432", "postgres_database": "fixture", "pinecone_index": "fixture"}
        self.config_patch = patch.object(auth, "authorized_config", return_value=({"sources": self.sources}, "upload-test"))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.service_config_patch = patch.object(assistant, "authorized_config", return_value=({"sources": self.sources}, "upload-test"))
        self.service_config_patch.start()
        self.addCleanup(self.service_config_patch.stop)
        self.clients = []
        self.observed = []

        main = ModuleType("main")
        main.DEFAULT_UPLOAD_QUESTION = "Pregunta por defecto de la prueba"
        main.ask_data_steward = self.backend
        main.multi_agent_app = SimpleNamespace(get_state=lambda _: SimpleNamespace(values={"evidences": {}}))
        agents = ModuleType("agents")
        agents.sql_agent = SimpleNamespace(PGHOST="fixture", PGPORT="5432", PGDATABASE="fixture")
        tools = ModuleType("tools")
        tools.rag_tool = SimpleNamespace(INDEX_NAME="fixture")
        modules = {
            "main": main, "agents": agents, "tools": tools,
            "tools.sql_scope": SimpleNamespace(CLIENT_SCOPE_ENFORCED=True),
            "tools.chart_context": SimpleNamespace(chart_units=ContextVar("fixture_chart_units")),
        }
        self.modules_patch = patch.dict("sys.modules", modules)
        self.modules_patch.start()
        self.addCleanup(self.modules_patch.stop)
        self.banking_patch = patch("api.services.banking.get_summary", return_value=None)
        self.banking_patch.start()
        self.addCleanup(self.banking_patch.stop)
        self.client, self.principal = self.make_client()
        self.conversation_id = self.client.post("/api/conversations").json()["conversation_id"]
        self.addCleanup(self.clean_sessions)

    def clean_sessions(self):
        for client, principal, token in self.clients:
            assistant.clear_session(principal.session_id)
            auth.logout(token)
            client.close()

    def make_client(self, profile="cliente"):
        token = secrets.token_urlsafe(40)
        permissions = ("institution:read",) if profile == "auditor" else ("assistant:use",)
        principal = auth.Principal(
            session_id=secrets.token_urlsafe(24), identity=f"fixture-{token[:8]}", display_name="Prueba de transporte",
            profile=profile, portal="cliente" if profile == "cliente" else "institucional",
            cliente_id=1 if profile == "cliente" else None, permissions=permissions,
            csrf_token=secrets.token_urlsafe(32), config_fingerprint="upload-test",
        )
        auth._SESSIONS[auth._token_hash(token)] = (principal, time.monotonic() + 60)
        auth._SESSION_IDS[principal.session_id] = auth._token_hash(token)
        client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
        client.cookies.set("banamex_test_session", token)
        client.headers["X-CSRF-Token"] = principal.csrf_token
        self.clients.append((client, principal, token))
        return client, principal

    def backend(self, question, **kwargs):
        paths = [Path(value) for value in (kwargs["files"] or [])]
        for path in paths:
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.observed.append({"question": question, "paths": paths, "bytes": [path.read_bytes() for path in paths], **kwargs})
        return {"final_answer": "Prueba de transporte completada; sin análisis documental.", "chart_path": None}

    def upload(self, files=None, question="", **fields):
        return self.client.post(
            uploads.DOCUMENT_PATH, data={"conversation_id": self.conversation_id, "question": question, **fields},
            files=files if files is not None else [("files", ("prueba.pdf", b"%PDF-fixture", "application/pdf"))],
        )

    def test_files_only_uses_default_and_server_identity_then_cleans_transport(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        call = self.observed[-1]
        self.assertTrue(call["question"].startswith("Pregunta por defecto de la prueba"))
        self.assertEqual((call["user_role"], call["cliente_id"], call["thread_id"], call["auto_open_chart"]), ("cliente", 1, self.conversation_id, False))
        self.assertEqual(call["bytes"], [b"%PDF-fixture"])
        self.assertTrue(all(not path.exists() and not path.parent.exists() for path in call["paths"]))
        self.assertEqual(set(response.json()), {"final_answer", "chart_id", "cutoff_date"})

    def test_multiple_files_preserve_question_and_cannot_escape_temp_directory(self):
        response = self.upload([
            ("files", ("../../prueba.pdf", b"%PDF-first", "application/pdf")),
            ("files", (r"C:\\otra\\prueba.pdf", b"%PDF-second", "application/pdf")),
            ("files", ("imagen.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")),
        ], "Resume estos documentos")
        self.assertEqual(response.status_code, 200, response.text)
        call = self.observed[-1]
        self.assertTrue(call["question"].startswith("Resume estos documentos"))
        self.assertEqual(len({path.name for path in call["paths"]}), 3)
        self.assertEqual(len({path.parent for path in call["paths"]}), 1)
        self.assertEqual(call["bytes"], [b"%PDF-first", b"%PDF-second", b"\x89PNG\r\n\x1a\nfixture"])

    def test_data_steward_uses_its_server_role_and_default_prompt(self):
        client, _ = self.make_client("data_steward")
        conversation_id = client.post("/api/conversations").json()["conversation_id"]
        response = client.post(uploads.DOCUMENT_PATH, data={"conversation_id": conversation_id}, files={"files": ("p.pdf", b"%PDF-fixture")})
        self.assertEqual(response.status_code, 200, response.text)
        call = self.observed[-1]
        self.assertEqual(call["question"], "Pregunta por defecto de la prueba")
        self.assertEqual((call["user_role"], call["cliente_id"], call["thread_id"]), ("universal", None, conversation_id))

    def test_plain_chat_remains_json_and_file_paths_are_not_accepted(self):
        response = self.client.post("/api/assistant/chat", json={"conversation_id": self.conversation_id, "question": "Consulta de prueba"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.observed[-1]["files"])
        response = self.client.post("/api/assistant/chat", json={"conversation_id": self.conversation_id, "question": "Consulta", "files": ["/etc/passwd"]})
        self.assertEqual(response.status_code, 422)

    def test_unsupported_empty_and_disguised_files_never_reach_backend(self):
        for name, content, expected in [("script.exe", b"%PDF-test", 415), ("vacio.pdf", b"", 400), ("falso.pdf", b"not a PDF", 415)]:
            with self.subTest(name=name):
                self.assertEqual(self.upload([("files", (name, content, "application/octet-stream"))]).status_code, expected)
        self.assertEqual(self.observed, [])

    def test_permissions_csrf_ownership_and_identity_fields(self):
        self.assertEqual(self.upload(user_role="universal").status_code, 400)
        self.assertEqual(self.upload(cliente_id="999").status_code, 400)
        self.assertEqual(self.upload(question="Consulta cliente 999").status_code, 403)
        self.client.headers.pop("X-CSRF-Token")
        self.assertEqual(self.upload().status_code, 403)
        other, _ = self.make_client()
        auditor, _ = self.make_client("auditor")
        for client, expected in [(other, 404), (auditor, 403)]:
            response = client.post(uploads.DOCUMENT_PATH, data={"conversation_id": self.conversation_id}, files={"files": ("p.pdf", b"%PDF-fixture")})
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.observed, [])

    def test_file_count_and_byte_limits(self):
        response = self.upload([("files", (f"p{i}.pdf", b"%PDF-fixture")) for i in range(6)])
        self.assertEqual(response.status_code, 400)
        # Small thresholds exercise the same streaming limits without large fixtures.
        with patch.object(uploads, "MAX_FILE_BYTES", 32), patch.object(uploads, "MAX_TOTAL_BYTES", 48):
            self.assertEqual(self.upload([("files", ("grande.pdf", b"%PDF-" + b"x" * 32))]).status_code, 413)
            self.assertEqual(self.upload([("files", (f"p{i}.pdf", b"%PDF-" + b"x" * 15)) for i in range(3)]).status_code, 413)
        with patch.object(uploads, "MAX_REQUEST_BYTES", 32):
            response = self.client.post(uploads.DOCUMENT_PATH, content=iter([b"x" * 64]), headers={"Content-Type": "multipart/form-data; boundary=test"})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.observed, [])

    def test_processing_failure_cleans_transport_and_never_returns_a_fake_answer(self):
        original = self.backend

        def failed_backend(question, **kwargs):
            original(question, **kwargs)
            raise RuntimeError("Fallo intencional de la prueba de transporte")

        with patch("main.ask_data_steward", side_effect=failed_backend):
            response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("final_answer", response.json())
        self.assertTrue(all(not path.exists() for path in self.observed[-1]["paths"]))


if __name__ == "__main__":
    unittest.main()
