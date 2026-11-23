"""
agents/sql_agent.py

Spoke especializado en análisis relacional Text-to-SQL para Banamex.
Conecta a la base de datos PostgreSQL (reto_banamex) y utiliza Google Gemini
incorporando la capa semántica, reglas de negocio bancarias y controles
estrictos de Gobernanza de Datos / RBAC (Row-Level Security):
  - Rol 'universal': Acceso analítico completo (solo lectura) a todo el esquema.
  - Rol 'cliente': Acceso restringido EXCLUSIVAMENTE a su propio `num_cte`
    (protección en doble capa: directiva de prompt + validador SQL determinista).
"""

import os
import re
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from dotenv import load_dotenv
from sqlalchemy import text

from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from langchain_community.utilities import SQLDatabase
from langsmith import traceable

from agents.llm import get_gemini_llm
from service_errors import public_service_error

load_dotenv(Path(__file__).parent.parent / ".env")

PGHOST = os.environ.get("PGHOST", "localhost")
PGPORT = os.environ.get("PGPORT", "5432")
PGUSER = os.environ.get("PGUSER", "postgres")
PGPASSWORD = os.environ.get("PGPASSWORD", "banamex_database")
PGDATABASE = os.environ.get("PGDATABASE", "reto_banamex")
SQL_MODEL = os.environ.get("SQL_MODEL", "gemini-3.5-flash")

DB_URI = f"postgresql+psycopg2://{PGUSER}:{PGPASSWORD}@{PGHOST}:{PGPORT}/{PGDATABASE}?options=-csearch_path=dim,cfg,public"

# Tablas que contienen datos sensibles de clientes, saldos o transacciones (requieren RLS en rol 'cliente')
RESTRICTED_TABLES = (
    "dim_cliente",
    "fact_sdos_captacoloca_m",
    "fact_txn_captacoloca_d",
)

# Palabras clave prohibidas en cualquier consulta (política estricta de solo lectura)
FORBIDDEN_SQL_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "COPY", "EXECUTE", "CALL", "MERGE"
)


def get_db_instance() -> SQLDatabase:
    return SQLDatabase.from_uri(DB_URI)


def get_sql_llm():
    """Retorna Gemini. Lanza ConfigError (error terminal) si falta GOOGLE_API_KEY."""
    return get_gemini_llm(SQL_MODEL)


def validate_cliente_id(raw_input: str) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]]]:
    """
    Valida que el ID ingresado por un cliente final sea un entero válido y exista
    en la tabla maestra `dim.dim_cliente` (columna `num_cte`, rango actual 1 a 5000).
    Utiliza consulta parametrizada para evitar SQL Injection.
    """
    cleaned = (raw_input or "").strip()
    if not cleaned.isdigit():
        return False, None, None

    cid = int(cleaned)
    if cid <= 0:
        return False, None, None

    db = get_db_instance()
    try:
        with db._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT num_cte, tipo_persona, genero_cte, segmento_cte, fec_alta "
                    "FROM dim.dim_cliente WHERE num_cte = :cid"
                ),
                {"cid": cid},
            ).fetchone()

        if not row:
            return False, None, None

        profile = {
            "num_cte": row[0],
            "tipo_persona": row[1],
            "genero_cte": row[2],
            "segmento_cte": row[3],
            "fec_alta": str(row[4]),
        }
        return True, cid, profile
    finally:
        db._engine.dispose()


def extract_clean_sql(text_raw: str) -> str:
    """Extrae la sentencia SQL limpia eliminando etiquetas markdown y explicaciones."""
    cleaned = text_raw.replace("[SQL]", "").strip()

    # Bloque de código ```sql ... ```
    match = re.search(r"```(?:sql)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        cleaned = match.group(1).strip()

    # Coincidencia con sentencia SQL (SELECT o WITH)
    match = re.search(r"\b(SELECT|WITH)\b.*?(?:;|\Z)", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(0).strip()

    lineas = [l for l in cleaned.splitlines() if not l.strip().startswith("###") and not l.strip().startswith("Given the")]
    return "\n".join(lineas).strip()


@traceable(run_type="tool", name="rbac_sql_validator")
def validate_sql_for_role(sql: str, user_role: str, cliente_id: Optional[int]) -> Tuple[bool, str]:
    """
    Validador determinista de Gobernanza de Datos (Capa 2 de defensa en profundidad).
    Verifica ANTES de ejecutar en PostgreSQL que:
      1. La sentencia sea estrictamente de solo lectura (SELECT / WITH) y única.
      2. Si `user_role == 'cliente'`, toda rama que consulte tablas de clientes, saldos
         o transacciones filtre obligatoriamente por `num_cte = {cliente_id}` sin bypasses.
    """
    sql_clean = sql.strip()
    # Eliminar comentarios SQL para evitar evasiones
    sql_no_comments = re.sub(r"--.*?$", "", sql_clean, flags=re.MULTILINE)
    sql_no_comments = re.sub(r"/\*.*?\*/", "", sql_no_comments, flags=re.DOTALL).strip()

    # 1. Bloquear múltiples sentencias separadas por punto y coma
    trimmed_semi = sql_no_comments.rstrip(";").strip()
    if ";" in trimmed_semi:
        return False, "Consulta bloqueada: no se permiten múltiples sentencias SQL."

    # 2. Verificar solo lectura
    if not re.match(r"^(SELECT|WITH)\b", trimmed_semi, flags=re.IGNORECASE):
        return False, "Consulta bloqueada: únicamente se permiten sentencias SELECT de solo lectura."

    for kw in FORBIDDEN_SQL_KEYWORDS:
        if re.search(rf"\b{kw}\b", trimmed_semi, flags=re.IGNORECASE):
            return False, f"Consulta bloqueada por política de solo lectura (operación no permitida: {kw})."

    # Si el rol es universal, solo aplican las reglas de solo lectura
    if user_role == "universal":
        return True, "OK"

    # 3. Validaciones estrictas de Row-Level Security para rol 'cliente'
    if user_role != "cliente" or cliente_id is None:
        return False, "Acceso denegado: sesión de usuario sin rol o cliente_id válido."

    # Bloquear patrones clásicos de bypass / SQL injection / acceso a otros clientes
    bypass_patterns = [
        r"\bOR\s+1\s*=\s*1\b",
        r"\bOR\s+TRUE\b",
        r"\bnum_cte\s*(!=|<>|<|>|<=|>=|\bIN\b|\bBETWEEN\b|\bIS\s+NOT\b)",
        r"\bNOT\s+[\w\.]*num_cte\b",
    ]
    for pat in bypass_patterns:
        if re.search(pat, trimmed_semi, flags=re.IGNORECASE):
            return False, "Acceso denegado por Secreto Bancario: patrón de filtro no permitido para rol cliente."

    # Revisar cada rama UNION / INTERSECT / EXCEPT de forma independiente
    branches = re.split(r"\b(?:UNION(?:\s+ALL)?|INTERSECT|EXCEPT)\b", trimmed_semi, flags=re.IGNORECASE)
    for branch in branches:
        touches_restricted = any(
            re.search(rf"\b{tbl}\b", branch, flags=re.IGNORECASE) for tbl in RESTRICTED_TABLES
        )
        if not touches_restricted:
            # Permite consultas puras a catálogos públicos (dim_producto, dim_txn)
            continue

        # Buscar todas las igualdades sobre num_cte (con o sin alias de tabla, ej. c.num_cte = 1)
        matches = re.findall(r"\b(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?num_cte\s*=\s*(\d+)\b", branch, flags=re.IGNORECASE)
        if not matches:
            return (
                False,
                f"Acceso denegado por Secreto Bancario (RBAC): como cliente (num_cte = {cliente_id}), "
                f"no puedes consultar datos globales ni de otros clientes; toda consulta sobre clientes, "
                f"saldos o transacciones debe limitarse exclusivamente a tu propio num_cte = {cliente_id}."
            )

        # Ninguna igualdad puede apuntar a otro cliente distinto al autenticado
        if any(int(m) != int(cliente_id) for m in matches):
            return (
                False,
                f"Acceso denegado por Secreto Bancario (RBAC): intento de acceso a un cliente distinto "
                f"a tu identificador autenticado (num_cte = {cliente_id})."
            )

        # Verificar que un OR de nivel superior en WHERE no anule el filtro num_cte = {cliente_id}
        where_match = re.search(r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|\Z)", branch, flags=re.DOTALL | re.IGNORECASE)
        if not where_match:
            return (
                False,
                f"Acceso denegado por Secreto Bancario (RBAC): falta cláusula WHERE con num_cte = {cliente_id}."
            )

        where_clause = where_match.group(1)
        # Comprobar que num_cte = {cliente_id} esté realmente dentro del WHERE (y no solo en un JOIN opcional)
        where_matches = re.findall(r"\b(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?num_cte\s*=\s*(\d+)\b", where_clause, flags=re.IGNORECASE)
        if not where_matches or any(int(m) != int(cliente_id) for m in where_matches):
            return (
                False,
                f"Acceso denegado por Secreto Bancario (RBAC): la restricción num_cte = {cliente_id} "
                f"debe aplicarse explícitamente en la cláusula WHERE."
            )

        # Detectar OR a nivel raíz (fuera de paréntesis) que podría ampliar filas fuera del cliente
        depth = 0
        top_level_chars = []
        for ch in where_clause:
            if ch == "(":
                depth += 1
                top_level_chars.append(" ")
            elif ch == ")":
                depth = max(0, depth - 1)
                top_level_chars.append(" ")
            elif depth == 0:
                top_level_chars.append(ch)
        top_level_where = "".join(top_level_chars)
        if re.search(r"\bOR\b", top_level_where, flags=re.IGNORECASE):
            return (
                False,
                f"Acceso denegado por Secreto Bancario (RBAC): no se permiten operadores OR sin paréntesis "
                f"que puedan eludir la restricción num_cte = {cliente_id}."
            )

    return True, "OK"


SQL_SYSTEM_PROMPT = """Eres el Especialista Técnico Text-to-SQL de la Plataforma de Datos de Banamex.
Tu única responsabilidad es generar consultas SQL en sintaxis PostgreSQL válidas, óptimas y precisas para responder la solicitud analítica.

================================================================================
ARQUITECTURA DE DATOS Y CAPA SEMÁNTICA BANAMEX (Modelo Estrella Kimball)
================================================================================

1. ESQUEMA DE DIMENSIONES (Contexto cualitativo, catálogo maestro):
   - `dim.dim_cliente` (5,000 registros):
     * `num_cte` (INTEGER, PK): Identificador único del cliente (enteros del 1 al 5000).
     * `fec_alta` (DATE): Fecha de ingreso del cliente.
     * `fec_nac` (DATE): Fecha de nacimiento.
     * `tipo_persona` (VARCHAR(10)): 'PF' (Persona Física, 96%) o 'PM' (Persona Moral, 4%).
     * `genero_cte` (VARCHAR(10)): 'HOMBRE' (52%), 'MUJER' (48%) o 'NO APLICA' (para PM).
     * `segmento_cte` (VARCHAR(20)): 'PREMIUM' (250), 'PREFERENTE' (1000), 'MASIVO' (3750).

   - `dim.dim_producto` (4 registros):
     * `cve_producto` (VARCHAR(40), PK): 'CHEQUES', 'AHORRO', 'C_PERSONAL', 'TDC'.
     * `cve_dominio` (VARCHAR(20)): 'DEPOSITS', 'LENDING', 'TDC'.
     * `grupo_producto` (VARCHAR(20)): 'CAPTACION' o 'COLOCACION'.
     * `descripcion` (VARCHAR(100)): Nombre comercial.

   - `dim.dim_txn` (9 registros):
     * `cve_producto`, `cve_txn` (100: DEPOSITO, 200: RETIRO, 300: PAGOS, 400: COMPRAS, 450: DISPOSICIONES).
     * `desc_txn` (VARCHAR(40)).

2. ESQUEMA DE HECHOS (Métricas cuantitativas y balances):
   - `cfg.fact_sdos_captacoloca_m` (Saldos mensuales, 120,000 filas):
     * Contiene 6 fotografías mensuales (cierres contables de 20260131 a 20260630).
     * Cada uno de los 5,000 clientes tiene 1 cuenta para cada uno de los 4 productos (20,000 cuentas activas por mes).
     * `num_cta` (INTEGER): Número de cuenta.
     * `num_cte` (INTEGER, FK -> dim_cliente.num_cte).
     * `cve_producto` (VARCHAR(40), FK -> dim_producto.cve_producto).
     * `saldo_apertura` (NUMERIC(18,2)): Saldo inicial del mes.
     * `saldo_mes` (NUMERIC(18,2)): Saldo de cierre del mes.
     * `fec_informacion` (INTEGER): Fecha de corte en formato YYYYMMDD (20260131, 20260228, 20260331, 20260430, 20260531, 20260630).

   - `cfg.fact_txn_captacoloca_d` (Transacciones diarias, 225,000 filas, Feb a Jun 2026):
     * `num_cta`, `num_cte`, `cve_producto`, `fec_txn`, `cve_mes_txn`, `saldo_txn` (importe positivo), `cve_txn`, `desc_txn`.

================================================================================
REGLAS DE NEGOCIO Y GOBERNANZA BANCARIA OBLIGATORIAS:
================================================================================
1. CONTEO DE CLIENTES:
   - Para saber cuántos clientes hay (por segmento, género, tipo de persona, etc.), SIEMPRE debes contar sobre la tabla maestra: `dim.dim_cliente` (ej: `SELECT COUNT(*) FROM dim.dim_cliente WHERE segmento_cte = 'PREMIUM'`).
   - NUNCA cuentes clientes sobre `fact_sdos_captacoloca_m` a menos que explícitamente se pida filtrar por clientes con cuentas con saldo > X.

2. FILTROS DE SEGMENTO:
   - Si la consulta pide clientes "PREMIUM", usa exactamente: `segmento_cte = 'PREMIUM'`. No uses `IN ('PREMIUM', 'PREFERENTE', 'MASIVO')` ni agrupes por todos si se pidió uno en específico.

3. FILTROS DE GÉNERO:
   - Los valores válidos son 'MUJER' y 'HOMBRE' en mayúsculas: `genero_cte = 'MUJER'`.

4. SALDOS TOTALES Y FECHAS DE INFORMACIÓN:
   - `fact_sdos_captacoloca_m` contiene 6 meses acumulados de fotos de saldo.
   - Si la consulta pide "saldo total actual" o no especifica mes, DEBES filtrar por el cierre más reciente:
     `WHERE fec_informacion = 20260630` (o `WHERE fec_informacion = (SELECT MAX(fec_informacion) FROM cfg.fact_sdos_captacoloca_m)`), de lo contrario sumarías 6 veces el dinero de los clientes.

5. DOMINIOS FINANCIEROS (Captación vs Colocación):
   - Captación (Depósitos de clientes): 'CHEQUES' y 'AHORRO' (o `p.grupo_producto = 'CAPTACION'`).
   - Colocación (Créditos y financiamiento): 'C_PERSONAL' y 'TDC' (o `p.grupo_producto = 'COLOCACION'`).

6. LLAVES FORÁNEAS Y JOINS:
   - Vincular saldos con clientes: `cfg.fact_sdos_captacoloca_m.num_cte = dim.dim_cliente.num_cte`
   - Vincular saldos con productos: `cfg.fact_sdos_captacoloca_m.cve_producto = dim.dim_producto.cve_producto`

7. FORMATO DE RESPUESTA:
   - Responde ÚNICAMENTE la consulta SQL limpia en PostgreSQL dentro de un bloque ```sql ... ```.
   - NO agregues saludos, explicaciones ni notas adicionales fuera de la consulta.
"""


CLIENT_RBAC_ADDENDUM = """
================================================================================
POLÍTICA ESTRICTA DE SEGURIDAD Y SECRETO BANCARIO (ROL: CLIENTE FINAL)
================================================================================
El usuario está autenticado EXCLUSIVAMENTE como el cliente con identificador:
    `num_cte = {cliente_id}`

REGLAS INQUEBRANTABLES (ROW-LEVEL SECURITY):
1. TODA consulta que acceda a `dim.dim_cliente`, `cfg.fact_sdos_captacoloca_m` o `cfg.fact_txn_captacoloca_d`
   DEBE incluir OBLIGATORIAMENTE en su cláusula `WHERE` la condición exacta:
       `num_cte = {cliente_id}`
   (o calificada con el alias de la tabla, por ejemplo `c.num_cte = {cliente_id}` o `f.num_cte = {cliente_id}`).
2. PROHIBIDO consultar datos de otros clientes, totales globales del banco, conteos de todos los clientes,
   distribución general de hombres/mujeres/segmentos o la evolución del saldo global de la cartera.
3. Si la solicitud pide información de otro `num_cte` o estadísticas globales de otros clientes,
   NO generes ninguna consulta SQL y responde ÚNICAMENTE el texto exacto:
       ACCESS_DENIED
4. Únicamente las tablas de catálogo público (`dim.dim_producto` y `dim.dim_txn`) pueden consultarse
   sin `num_cte` cuando el cliente pregunte por productos o tipos de transacciones que ofrece el banco.
"""


@traceable(run_type="chain", name="sql_agent_pipeline")
def run_sql_agent(
    instruction: str,
    user_role: str = "universal",
    cliente_id: Optional[int] = None,
) -> dict:
    """
    Ejecuta el pipeline Text-to-SQL con Gemini aplicando gobernanza RBAC:
    1. Construye el prompt según el rol ('universal' o 'cliente').
    2. Genera y extrae la consulta SQL limpia.
    3. Valida la consulta de forma determinista con `validate_sql_for_role` antes de tocar PostgreSQL.
    4. Ejecuta en PostgreSQL reto_banamex y retorna el resultado.
    """
    role = (user_role or "universal").lower()

    # Validación temprana de sesión para rol cliente
    if role == "cliente" and cliente_id is None:
        denial = "Acceso denegado por Gobernanza de Datos: sesión de cliente sin identificador `num_cte`."
        return {
            "sql_query": "BLOQUEADA (SIN CLIENTE_ID)",
            "raw_result": None,
            "summary": denial,
            "error": "ACCESS_DENIED",
        }

    # Fuera del try: un error de configuración es terminal y no debe convertirse en evidencia.
    llm = get_sql_llm()
    db = get_db_instance()
    clean_sql = None

    try:
        system_prompt = SQL_SYSTEM_PROMPT
        if role == "cliente":
            system_prompt += CLIENT_RBAC_ADDENDUM.format(cliente_id=cliente_id)

        user_message = f"Instrucción para generar SQL: {instruction}"
        if role == "cliente":
            user_message += f"\n(Recuerda: usuario autenticado exclusivamente como cliente num_cte = {cliente_id})."

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ]
        raw_response = llm.invoke(messages)
        content = raw_response.content
        if isinstance(content, list):
            raw_text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        else:
            raw_text = str(content)

        # Capa 1: El modelo detectó violación de Secreto Bancario
        if "ACCESS_DENIED" in raw_text.upper():
            denial_msg = (
                f"ACCESO DENEGADO (Política de Secreto Bancario y Gobernanza RBAC): "
                f"Tu perfil de Cliente Final (num_cte = {cliente_id}) únicamente tiene autorización "
                f"para consultar tus propios datos, cuentas, saldos y transacciones. No tienes permisos "
                f"para acceder a métricas globales del banco ni a información de otros clientes."
            )
            return {
                "sql_query": "BLOQUEADA POR POLÍTICA RBAC (ACCESS_DENIED)",
                "raw_result": None,
                "summary": denial_msg,
                "error": "ACCESS_DENIED",
            }

        clean_sql = extract_clean_sql(raw_text)
        if not clean_sql:
            raise ValueError(f"No se pudo extraer SQL ejecutable de la respuesta: {raw_text}")

        # Capa 2: Validador determinista en Python antes de ejecutar en PostgreSQL
        is_allowed, reason = validate_sql_for_role(clean_sql, role, cliente_id)
        if not is_allowed:
            return {
                "sql_query": f"BLOQUEADA POR VALIDADOR RBAC:\n{clean_sql}",
                "raw_result": None,
                "summary": reason,
                "error": "ACCESS_DENIED",
            }

        # Ejecución segura en PostgreSQL
        db_result = db.run(clean_sql)

        evidence_summary = (
            f"Consulta SQL ejecutada (Rol: {role}"
            + (f", num_cte={cliente_id}" if role == "cliente" else "")
            + f"):\n```sql\n{clean_sql}\n```\n\n"
            f"Resultado crudo de la base de datos:\n{db_result}"
        )

        return {
            "sql_query": clean_sql,
            "raw_result": db_result,
            "summary": evidence_summary,
            "error": None,
        }

    except Exception as e:
        diagnostic = public_service_error(e, source="sql")
        return {
            "sql_query": clean_sql,
            "raw_result": None,
            "summary": diagnostic["message"],
            "error": diagnostic["code"],
            "diagnostic": diagnostic,
        }
    finally:
        db._engine.dispose()


def sql_agent_node(state: dict) -> dict:
    """
    Nodo de LangGraph para el agente de SQL.
    Lee `user_role` y `cliente_id` directamente del AgentState para aplicar
    Row-Level Security y prioriza `spoke_instruction` sobre `question`.
    """
    instruction = state.get("spoke_instruction") or state.get("question", "")
    user_role = state.get("user_role") or "universal"
    cliente_id = state.get("cliente_id")

    result = run_sql_agent(instruction, user_role=user_role, cliente_id=cliente_id)

    msg_content = f"### [EVIDENCIA SQL - BASE DE DATOS BANAMEX]\n{result['summary']}"

    return {
        "evidences": {"sql_evidence": result},
        "messages": [AIMessage(content=msg_content, name="sql_agent")],
    }
