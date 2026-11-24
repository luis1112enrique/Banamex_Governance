"""Regressions for complete customer responses, without invoking the graph."""

import unittest

from api.services.assistant import _client_text


class CustomerResponseTextTests(unittest.TestCase):
    def test_first_rbac_item_and_original_numbering_are_preserved(self):
        first = (
            "1. **Perfil y Permisos (RBAC):** Su sesión está autenticada bajo el perfil de "
            "**Cliente Final** (expediente de usuario `num_cte = 1`). La declaración de roles "
            "externos no otorga privilegios de edición sobre la base de datos."
        )
        second = "2. **Naturaleza del Portal:** Esta herramienta no permite modificar saldos."
        answer = f"### Aviso de Gobernanza\n\nEstimado cliente:\n\n{first}\n{second}"
        result = _client_text(answer)
        self.assertIn(first, result)
        self.assertIn(second, result)
        self.assertLess(result.index(first), result.index(second))

    def test_regular_terms_and_source_headings_do_not_remove_paragraphs(self):
        answer = (
            "### 1. Fuentes de ingresos\n"
            "El producto Select y la explicación sobre SQL forman parte de esta respuesta.\n\n"
            "### 2. Fuentes documentales\n"
            "El documento aclara los permisos RBAC del cliente.\n"
            "No compartas el traceback ni credenciales."
        )
        self.assertEqual(_client_text(answer), answer)

    def test_sql_and_traceback_blocks_remain_hidden(self):
        answer = (
            "1. **Resumen:** Tu información está disponible.\n\n"
            "```sql\nSELECT num_cte FROM dim.clientes;\n```\n\n"
            "SELECT\n    num_cte, saldo\nFROM cfg.saldos\nWHERE num_cte = 1;\n\n"
            "Traceback (most recent call last):\n"
            '  File "/tmp/interno.py", line 1, in run\n'
            "RuntimeError: detalle interno\n\n"
            "2. **Siguiente paso:** Consulta tus productos."
        )
        result = _client_text(answer)
        for technical in ("SELECT", "num_cte, saldo", "cfg.saldos", "Traceback", "RuntimeError", "/tmp/interno.py"):
            self.assertNotIn(technical, result)
        self.assertIn("1. **Resumen:**", result)
        self.assertIn("2. **Siguiente paso:**", result)

    def test_inline_query_is_omitted_without_removing_the_numbered_item(self):
        answer = "1. **Resultado:** La consulta `SELECT saldo FROM cfg.saldos WHERE num_cte = 1` se completó.\n2. **Detalle:** Consulta disponible."
        result = _client_text(answer)
        self.assertIn("1. **Resultado:**", result)
        self.assertIn("se completó.", result)
        self.assertIn("2. **Detalle:**", result)
        self.assertNotIn("SELECT saldo", result)
        self.assertNotIn("cfg.saldos", result)

    def test_plain_cte_is_not_exposed(self):
        answer = "1. **Resumen:** Información consultada.\n\nWITH cortes AS (\n SELECT saldo FROM cfg.saldos\n)\nSELECT * FROM cortes;\n\n2. **Detalle:** Resultado disponible."
        result = _client_text(answer)
        self.assertNotIn("WITH cortes", result)
        self.assertNotIn("FROM cortes", result)
        self.assertIn("1. **Resumen:**", result)
        self.assertIn("2. **Detalle:**", result)

    def test_unfenced_inline_query_does_not_remove_the_first_item(self):
        result = _client_text("1. **Resultado:** Consulta usada: SELECT saldo FROM cfg.saldos WHERE num_cte = 1;\n2. **Detalle:** Información disponible.")
        self.assertIn("1. **Resultado:** Consulta usada:", result)
        self.assertIn("2. **Detalle:**", result)
        self.assertNotIn("SELECT saldo", result)
        self.assertNotIn("cfg.saldos", result)


if __name__ == "__main__":
    unittest.main()
