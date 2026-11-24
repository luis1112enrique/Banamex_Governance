"""
agents/coordinator.py

HUB / ORQUESTADOR CENTRAL (Data Steward Supervisor).
Responsable de:
1. Analizar la intención del usuario considerando todo el historial de la conversación.
2. BLINDAJE DE DOMINIO: Rechazar directamente consultas ajenas al ámbito financiero y bancario de Banamex.
3. Discernir si la nueva pregunta se puede responder DIRECTAMENTE con la memoria existente
   o si requiere delegar a un spoke especializado.
4. Enriquecer la consulta técnica mediante el canal 'spoke_instruction' para evitar ambigüedades.
5. Rutear al Spoke correcto:
   - 'sql_agent': Datos relacionales y métricas financieras en PostgreSQL.
   - 'rag_agent': Documentación oficial, contratos, comisiones y normativas en Pinecone.
   - 'FINISH': Saludos, aclaraciones de memoria o consultas fuera de dominio.
6. Sintetizar la respuesta final con enfoque de Data Stewardship y gobernanza.
"""

import os
import json
from pathlib import Path
from typing import Sequence, Dict, Any, Optional
from dotenv import load_dotenv

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, BaseMessage
from langchain_ollama import ChatOllama
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import traceable

from tools.chart_tool import build_chart_spec_from_sql

load_dotenv(Path(__file__).parent.parent / ".env")

COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", "gemini-3.5-flash")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")


def get_coordinator_llm():
    """Retorna Gemini si GOOGLE_API_KEY está configurada, o Ollama como fallback local."""
    if GOOGLE_API_KEY:
        return ChatGoogleGenerativeAI(
            model=COORDINATOR_MODEL,
            google_api_key=GOOGLE_API_KEY,
            temperature=0
        )
    return ChatOllama(model=COORDINATOR_MODEL, temperature=0)


def extract_content_text(response) -> str:
    """Extrae texto plano del contenido de respuesta, soportando bloques multimodales y strings."""
    content = response.content if hasattr(response, "content") else response
    if isinstance(content, list):
        text = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    else:
        text = str(content)
    return clean_degenerative_repetitions(text)


def clean_degenerative_repetitions(text: str) -> str:
    """Detecta y poda bucles de repetición degenerativa generados accidentalmente por modelos de lenguaje."""
    lines = text.split("\n")
    cleaned_lines = []
    repeat_tracker = []

    for line in lines:
        stripped = line.strip()
        if stripped and len(stripped) > 5 and stripped in repeat_tracker[-3:]:
            continue
        cleaned_lines.append(line)
        if stripped:
            repeat_tracker.append(stripped)

    return "\n".join(cleaned_lines)


def format_conversation_history(messages: Sequence[BaseMessage]) -> str:
    """Formatea el historial de mensajes para que el LLM tenga contexto de turnos anteriores."""
    if not messages:
        return "No hay conversación previa."
    history = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            history.append(f"Usuario: {msg.content}")
        elif isinstance(msg, AIMessage):
            speaker = getattr(msg, "name", None) or "Coordinador"
            content = str(msg.content)
            if content.startswith("### [EVIDENCIA"):
                continue
            history.append(f"{speaker}: {content.strip()}")
    return "\n".join(history) if history else "No hay conversación previa."


def format_user_context(user_role: Optional[str], cliente_id: Optional[int]) -> str:
    """Construye el bloque de gobernanza RBAC según el rol autenticado en AgentState."""
    role = (user_role or "universal").lower()
    if role == "cliente" and cliente_id is not None:
        return (
            f"ROL AUTENTICADO: CLIENTE FINAL DE BANAMEX (num_cte = {cliente_id}).\n"
            f"REGLAS ESTRICTAS DE GOBERNANZA Y SECRETO BANCARIO (RBAC):\n"
            f"- Este cliente ÚNICAMENTE puede consultar SUS propios datos, cuentas, saldos y transacciones "
            f"(filtrando SIEMPRE por num_cte = {cliente_id}), así como catálogos públicos de productos/transacciones "
            f"y documentación oficial en Pinecone (comisiones, contratos, términos).\n"
            f"- PROHIBIDO revelar información de otros clientes (otro num_cte), conteos totales de clientes del banco, "
            f"distribución global por género/segmento o saldos globales de la cartera.\n"
            f"- Si el cliente pregunta por otros clientes o métricas globales de la base de datos, DEBES rechazar "
            f"directamente la solicitud con 'can_answer_directly': true y 'next_agent': 'FINISH', explicando "
            f"cortésmente que por política de Secreto Bancario y Gobernanza de Datos (RBAC) su perfil de Cliente "
            f"Final solo tiene acceso a su propio expediente (num_cte = {cliente_id}).\n"
            f"- Cuando delegues consultas personales al 'sql_agent' (ej. 'mi saldo', 'mis cuentas', 'mis movimientos', "
            f"'mi segmento'), DEBES incluir explícitamente `num_cte = {cliente_id}` en 'spoke_instruction'."
        )
    return (
        "ROL AUTENTICADO: USUARIO UNIVERSAL (Data Steward / Analista Interno).\n"
        "- Tiene autorización analítica de solo lectura sobre toda la base de datos SQL "
        "(todos los clientes, segmentos, saldos globales y transacciones) y toda la documentación oficial en Pinecone."
    )


MAX_MULTI_HOP_STEPS = 3

CHART_INTENT_KEYWORDS = (
    "grafic", "gráfic", "chart", "plot", "diagrama", "visualiz",
    "barras", "pastel", "histograma", "curva", "plotea",
)


def _question_requests_chart(question: str) -> bool:
    """Detecta si el usuario solicitó explícitamente una representación gráfica."""
    q_lower = (question or "").lower()
    return any(kw in q_lower for kw in CHART_INTENT_KEYWORDS)


def _format_evidences(evidences: Optional[dict]) -> str:
    """Formatea el diccionario de evidencias acumuladas en el turno actual."""
    if not evidences:
        return "Ninguna todavía en este turno."
    parts = []
    for k, v in evidences.items():
        summary = v.get("summary", "") if isinstance(v, dict) else str(v)
        parts.append(f"--- [{k.upper()}] ---\n{summary}")
    return "\n\n".join(parts) if parts else "Ninguna todavía en este turno."


# ==============================================================================
# PROMPT DE DELEGACIÓN MULTI-HOP, BLINDAJE DE DOMINIO Y ENRIQUECIMIENTO
# ==============================================================================

ROUTING_PROMPT = """Eres el Data Steward y Coordinador Principal de la Plataforma de Datos de Grupo Financiero Banamex.
Tu rol es supervisar la conversación, razonar sobre el contexto (soportando flujos de varios pasos / multi-hop) y tomar una decisión entre:
A) Responder DIRECTAMENTE si la pregunta puede contestarse con el historial previo, si es un saludo, si viola permisos RBAC, o si está FUERA DE DOMINIO.
B) Delegar a un SPOKE ESPECIALIZADO enriqueciendo la consulta técnica.

CONTEXTO DE AUTENTICACIÓN Y GOBERNANZA (RBAC):
{user_context}

HISTORIAL DE LA CONVERSACIÓN PREVIA:
{history}

EVIDENCIA YA RECOPILADA EN ESTE TURNO:
{current_evidences}

PREGUNTA ACTUAL DEL USUARIO:
{question}

POLÍTICA ESTRICTA DE GOBERNANZA Y DOMINIO BANCARIO:
- Eres el Data Steward exclusivo de Grupo Financiero Banamex.
- Si la pregunta del usuario es AJENA al ámbito de Banamex (por ejemplo: deportes, partidos, entretenimiento, recetas, clima, política externa o cultura general no financiera), DEBES:
  1. Definir "can_answer_directly": true
  2. Definir "next_agent": "FINISH"
  3. Redactar en "direct_answer" una negativa cortés y profesional indicando que como Data Steward Oficial de Grupo Financiero Banamex únicamente tienes facultades para atender consultas sobre datos de clientes, cuentas, transacciones, contratos, comisiones y normativas oficiales de Banamex.

SPOKES ESPECIALIZADOS DISPONIBLES:
1. 'sql_agent':
   - Usar para métricas cuantitativas, agrupaciones, conteos y saldos en la base de datos relacional PostgreSQL de Banamex.
   - Entidades: clientes (segmentos PREMIUM, PREFERENTE, MASIVO; género, tipo persona), productos de captación y colocación, saldos mensuales (20260131 a 20260630) y movimientos diarios.
   - Ejemplos: "¿Cuántos clientes tenemos?", "¿Cuántos son mujeres?", "¿Qué cuentas puede tener un cliente?".

2. 'rag_agent':
   - Usar para buscar en la documentación oficial de Banamex indexada en Pinecone (contratos marco, comisiones, CAT, requisitos normativos oficiales, folletos de productos).
   - Ejemplos: "¿Cuáles son las comisiones de la tarjeta Costco?", "¿Qué comisiones cobra MiCuenta Banamex?", "¿Cuáles son los requisitos oficiales para contratar un Crédito Personal o abrir cuenta?".

3. 'chart_tool':
   - Usar para generar una gráfica corporativa en formato PNG (Matplotlib) cuando el usuario pida explícitamente una gráfica, visualización, barras, líneas o pastel.
   - REGLA CRÍTICA DE ORDEN (MULTI-HOP):
     * Si el usuario pide una gráfica de datos que AÚN NO están en `EVIDENCIA YA RECOPILADA EN ESTE TURNO` ni en el `HISTORIAL DE LA CONVERSACIÓN PREVIA`, **DEBES delegar PRIMERO a `'sql_agent'`** para obtener las cifras reales (NUNCA inventes datos).
     * Una vez que `EVIDENCIA YA RECOPILADA EN ESTE TURNO` (o el historial previo) YA contiene los números exactos, delega a `'chart_tool'` colocando en `'spoke_instruction'` un objeto JSON válido con esta estructura exacta:
       {{
         "chart_type": "line" | "bar" | "grouped_bar" | "pie",
         "title": "Título claro y ejecutivo de la gráfica",
         "x_label": "Etiqueta del eje X (ej. Mes de Corte, Producto, Segmento)",
         "y_label": "Etiqueta del eje Y (ej. Saldo Total (MXN), Número de Clientes)",
         "value_prefix": "$" (para montos en pesos) o "" (para conteos),
         "labels": ["Ene 2026", "Feb 2026", ...],
         "series": [
           {{"name": "Nombre de la serie", "values": [12345.67, 23456.78, ...]}}
         ]
       }}
     * Usa EXACTAMENTE los valores numéricos reales obtenidos en `sql_evidence` (sin redondeos inventados ni datos faltantes).

4. 'doc_agent':
   - Usar para analizar, clasificar e inventariar documentos y archivos cargados por el cliente (imágenes, credenciales INE, comprobantes de domicilio, recibos de nómina en inputs/).
   - Extrae qué tipo de documento es, su titular, vigencia y campos clave estructurados.
   - REGLA CRÍTICA DE ORDEN MULTI-HOP (TRÁMITES DE CRÉDITO Y EXPEDIENTES):
     * Si el usuario cargó archivos y pregunta si puede tramitar un crédito, abrir una cuenta o qué le falta:
       Paso 1: Si `doc_evidence` NO existe todavía en este turno, **DEBES delegar PRIMERO a `'doc_agent'`** para obtener el inventario de lo que el cliente subió.
       Paso 2: Una vez que `doc_evidence` ya existe, si la consulta requiere contrastar contra los requisitos oficiales de Banamex (ej. Crédito Personal, Cuenta Perfiles), **DEBES delegar a `'rag_agent'`** para recuperar el checklist normativo oficial.
       Paso 3: Con ambos resultados (`doc_evidence` y `rag_evidence`), el Coordinador realiza la síntesis final de cotejo (Checklist Matching).

5. 'FINISH' (Respuesta directa con memoria, RBAC o fuera de dominio):
   - Usar cuando la pregunta se puede responder con lo que YA se dijo en el historial previo, para saludos/despedidas, para rechazos RBAC o para consultas fuera de dominio bancario.

INSTRUCCIONES DE ENRIQUECIMIENTO:
- Si decides delegar a `'sql_agent'`, `'rag_agent'` o `'doc_agent'`, genera en `'spoke_instruction'` una instrucción técnica COMPLETA y SIN AMBIGÜEDADES, resolviendo pronombres y referencias anteriores.
  * Ejemplo SQL: "Consultar en cfg.fact_sdos_captacoloca_m el saldo mensual de AHORRO y CHEQUES."
  * Ejemplo RAG: "Requisitos oficiales y documentos exigidos para solicitud de Crédito Personal Banamex en contratos y folletos oficiales."
  * Ejemplo Doc Agent: "Inspecciona y clasifica los documentos cargados por el cliente, extrayendo titular, vigencia y tipo de documento."

FORMATO DE RESPUESTA OBLIGATORIO:
Responde ÚNICAMENTE un objeto JSON válido con la siguiente estructura (sin texto antes ni después):
{{
  "can_answer_directly": true | false,
  "direct_answer": "Respuesta completa si can_answer_directly es true; de lo contrario null",
  "next_agent": "sql_agent" | "rag_agent" | "chart_tool" | "doc_agent" | "FINISH",
  "plan": "Explicación breve de 1 línea de tu decisión",
  "spoke_instruction": "Instrucción técnica para el spoke correspondiente (o null si direct_answer)"
}}
"""

SYNTHESIS_PROMPT = """Eres el Data Steward Oficial de Grupo Financiero Banamex.
Tu misión es presentar una respuesta clara, confiable, profesional y bien estructurada al usuario,
basándote en la evidencia técnica recopilada por tu equipo de spokes especializados.

CONTEXTO DE AUTENTICACIÓN Y GOBERNANZA (RBAC):
{user_context}

PREGUNTA DEL USUARIO:
{question}

HISTORIAL CONVERSACIONAL:
{history}

EVIDENCIA RECIÉN RECOPILADA:
{evidence}

REGLAS DE GOBERNANZA, ESTRUCTURA Y COMUNICACIÓN BANCARIA:
1. Precisión matemática, citas documentales y gráficas:
   - Si la evidencia proviene de SQL, cita las cifras y tablas exactas (ej. dim.dim_cliente, cfg.fact_sdos_captacoloca_m).
   - Si se generó una gráfica (`CHART_EVIDENCE`), confirma explícitamente el título de la gráfica generada, el tipo de visualización y la ruta del archivo PNG guardado, interpretando las tendencias o hallazgos clave visibles en los datos.
   - Si la evidencia indica ACCESO DENEGADO por RBAC/Secreto Bancario, informa con claridad y cortesía al cliente que no cuenta con permisos para consultar esa información y recuerda que su acceso se limita a su propio num_cte.
   - Si la evidencia proviene de RAG (Pinecone), cita explícitamente el documento oficial (ej. anexo_costco.pdf, contrato_cuenta_switch.pdf), las páginas de referencia y los montos exactos en pesos o porcentajes de comisiones/CAT.
   - Si la evidencia contiene tanto `doc_evidence` (inventario de documentos del cliente) como `rag_evidence` (requisitos oficiales del banco):
     * Realiza un COTEJO FORMAL Y RIGUROSO (Checklist Matching).
     * Desglosa en una tabla comparativa clara:
       | Requisito Oficial Banamex | Documento Presentado | Estado (Cumple / Falta / Vencido) | Observaciones |
     * Dictamina con total claridad en la conclusión si el trámite o crédito procede, o especifica con viñetas los documentos exactos que faltan para completar la solicitud.
2. Trazabilidad de origen: Menciona claramente de qué fuente oficial provino el dato.
3. Estructura concisa y limpia (MÁXIMO 3 SECCIONES):
   - Sección 1: Fuente y certificación del dato.
   - Sección 2: Resultados detallados (usa viñetas limpias o tablas Markdown; menciona la gráfica generada si aplica).
   - Sección 3: Conclusión ejecutiva / Dictamen de gobernanza (máximo 1 párrafo breve).
4. REGLA ESTRICTA ANTI-REPETICIÓN:
   - PROHIBIDO repetir secciones, encabezados o frases ya escritas.
   - Concluye de inmediato con la firma del Data Steward y finaliza la respuesta.
"""


@traceable(run_type="chain", name="hub_routing_decision")
def route_or_respond(
    question: str,
    messages: Sequence[BaseMessage],
    evidences: Optional[dict] = None,
    user_role: Optional[str] = "universal",
    cliente_id: Optional[int] = None,
) -> dict:
    """
    Invoca a Gemini para decidir si la pregunta se resuelve directamente (memoria, RBAC o fuera de dominio)
    o si se delega al siguiente Spoke ('sql_agent', 'rag_agent' o 'chart_tool').
    """
    llm = get_coordinator_llm()
    history_str = format_conversation_history(messages)
    user_context_str = format_user_context(user_role, cliente_id)
    evidences_str = _format_evidences(evidences)

    prompt = ROUTING_PROMPT.format(
        user_context=user_context_str,
        history=history_str,
        current_evidences=evidences_str,
        question=question,
    )
    response = llm.invoke([
        SystemMessage(content="Eres el Supervisor de Gobernanza y Orquestador de Banamex. Responde estrictamente en formato JSON."),
        HumanMessage(content=prompt)
    ])

    raw_text = extract_content_text(response).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        if raw_text.endswith("```"):
            raw_text = raw_text.rsplit("```", 1)[0]
    raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
        can_answer = parsed.get("can_answer_directly", False)
        next_agent = parsed.get("next_agent", "FINISH")

        if can_answer or next_agent == "FINISH":
            return {
                "can_answer_directly": True,
                "direct_answer": parsed.get("direct_answer") or "Consulta resuelta directamente.",
                "next_agent": "FINISH",
                "plan": parsed.get("plan", "Respuesta directa de gobernanza o memoria."),
                "spoke_instruction": None
            }

        if next_agent not in ["sql_agent", "rag_agent", "chart_tool", "doc_agent"]:
            next_agent = "sql_agent"

        raw_instruction = parsed.get("spoke_instruction")
        if isinstance(raw_instruction, (dict, list)):
            spoke_instruction = json.dumps(raw_instruction, ensure_ascii=False)
        else:
            spoke_instruction = raw_instruction or question

        return {
            "can_answer_directly": False,
            "direct_answer": None,
            "next_agent": next_agent,
            "plan": parsed.get("plan", "Ruteo hacia spoke especializado."),
            "spoke_instruction": spoke_instruction
        }

    except Exception:
        # Fallback de seguridad heurístico
        q_lower = question.lower()
        if any(w in q_lower for w in ["cuantos", "cuántos", "saldo", "cliente", "transaccion", "cuenta", "premium", "grafic", "gráfic"]):
            return {
                "can_answer_directly": False,
                "direct_answer": None,
                "next_agent": "sql_agent",
                "plan": "Ruteo a análisis relacional por presencia de términos cuantitativos.",
                "spoke_instruction": question
            }
        if any(w in q_lower for w in ["comision", "comisión", "contrato", "cat", "anualidad", "terminos", "términos", "requisito", "costco"]):
            return {
                "can_answer_directly": False,
                "direct_answer": None,
                "next_agent": "rag_agent",
                "plan": "Ruteo a documentación oficial de Banamex.",
                "spoke_instruction": question
            }
        return {
            "can_answer_directly": True,
            "direct_answer": "Como Data Steward Oficial de Grupo Financiero Banamex, estoy a tu disposición para consultas analíticas sobre clientes, saldos, transacciones, normativas y contratos oficiales del banco.",
            "next_agent": "FINISH",
            "plan": "Respuesta institucional de Data Steward.",
            "spoke_instruction": None
        }


@traceable(run_type="chain", name="hub_final_synthesis")
def synthesize_response(
    question: str,
    evidences: dict,
    messages: Sequence[BaseMessage],
    user_role: Optional[str] = "universal",
    cliente_id: Optional[int] = None,
) -> str:
    """Sintetiza la respuesta final del Data Steward con la evidencia documental, transaccional y gráfica recopilada."""
    llm = get_coordinator_llm()
    history_str = format_conversation_history(messages)
    user_context_str = format_user_context(user_role, cliente_id)
    evidence_text = _format_evidences(evidences)

    if not evidences:
        evidence_text = "No se requirieron fuentes externas para esta consulta."

    prompt = SYNTHESIS_PROMPT.format(
        user_context=user_context_str,
        question=question,
        history=history_str,
        evidence=evidence_text,
    )
    response = llm.invoke([
        SystemMessage(content="Eres el Oficial Data Steward de Banamex. Redacta la respuesta final con máxima precisión y claridad."),
        HumanMessage(content=prompt)
    ])
    return extract_content_text(response)


def coordinator_node(state: dict) -> dict:
    """
    Nodo del Coordinador (Hub) con soporte Multi-Hop:
    - Al inicio de un turno: analiza la pregunta y decide si responder directo o delegar al 1er Spoke.
    - Al regresar de un Spoke:
        * Si proviene de 'sql_agent' con datos válidos, el usuario solicitó una gráfica y todavía no
          se ha ejecutado 'chart_tool' (y step_count < MAX_MULTI_HOP_STEPS), encadena el 2do salto
          hacia 'chart_tool' pasando la especificación JSON sin borrar `sql_evidence`.
        * En el resto de casos (incluyendo retorno de 'chart_tool', 'rag_agent', 'ACCESS_DENIED' o
          límite de saltos alcanzado), sintetiza `final_answer` y cierra en `FINISH` preservando `chart_path`.
    """
    messages = state.get("messages", [])
    question = state.get("question", "")
    evidences = state.get("evidences", {}) or {}
    user_role = state.get("user_role") or "universal"
    cliente_id = state.get("cliente_id")
    step_count = state.get("step_count") or 0

    last_message = messages[-1] if messages else None
    last_spoke = getattr(last_message, "name", None) if last_message else None
    is_spoke_return = last_spoke in ["sql_agent", "rag_agent", "chart_tool", "doc_agent"]

    # --------------------------------------------------------------------------
    # RETORNO DE UN SPOKE (Evaluación Multi-Hop o Síntesis Final)
    # --------------------------------------------------------------------------
    if is_spoke_return and evidences:
        sql_ev = evidences.get("sql_evidence", {})
        sql_succeeded = isinstance(sql_ev, dict) and not sql_ev.get("error")

        # Encadenamiento Multi-Hop 1: SQL -> Orquestador -> Chart Tool (Optimizado a CERO TOKENS)
        if (
            last_spoke == "sql_agent"
            and sql_succeeded
            and "chart_evidence" not in evidences
            and step_count < MAX_MULTI_HOP_STEPS
            and _question_requests_chart(question)
        ):
            # Optimización CERO-TOKENS: extracción determinista directa de SQL estructurado a Chart Spec
            structured_data = sql_ev.get("structured_data")
            chart_spec = None
            if structured_data and isinstance(structured_data, dict):
                cols = structured_data.get("columns", [])
                rows = structured_data.get("rows", [])
                if cols and rows:
                    chart_spec = build_chart_spec_from_sql(cols, rows, question=question)

            if chart_spec:
                return {
                    "plan": "Encadenamiento determinista SQL -> Chart Tool con datos estructurados (Cero Tokens de LLM).",
                    "next_agent": "chart_tool",
                    "spoke_instruction": json.dumps(chart_spec, ensure_ascii=False),
                    "step_count": step_count + 1,
                    "final_answer": None,
                }

            # Fallback a enrutamiento por LLM si el parser determinista no aplica
            followup = route_or_respond(
                question,
                messages,
                evidences=evidences,
                user_role=user_role,
                cliente_id=cliente_id,
            )
            if followup["next_agent"] == "chart_tool":
                return {
                    "plan": followup["plan"],
                    "next_agent": "chart_tool",
                    "spoke_instruction": followup["spoke_instruction"],
                    "step_count": step_count + 1,
                    "final_answer": None,
                }

        # Encadenamiento Multi-Hop 2: Doc Agent -> Orquestador -> RAG Agent
        # Tras clasificar los archivos del cliente, si se evalúa un trámite (ej. crédito, cuenta),
        # se consulta inmediatamente a RAG Agent para obtener los requisitos normativos oficiales.
        if (
            last_spoke == "doc_agent"
            and "doc_evidence" in evidences
            and "rag_evidence" not in evidences
            and step_count < MAX_MULTI_HOP_STEPS
        ):
            followup = route_or_respond(
                question,
                messages,
                evidences=evidences,
                user_role=user_role,
                cliente_id=cliente_id,
            )
            if followup["next_agent"] == "rag_agent":
                return {
                    "plan": followup["plan"],
                    "next_agent": "rag_agent",
                    "spoke_instruction": followup["spoke_instruction"],
                    "step_count": step_count + 1,
                    "final_answer": None,
                }

        # Pass-Through Inteligente para RAG Spoke Puro (sin cruce con SQL, Chart o Doc Agent):
        # El rag_agent ya sintetizó la evidencia con Gemini 3.6 Flash y máximo rigor contractual.
        # Evitamos una segunda llamada redundante al LLM que consumiría tokens y podría alterar montos/cláusulas.
        if (
            last_spoke == "rag_agent"
            and "sql_evidence" not in evidences
            and "chart_evidence" not in evidences
            and "doc_evidence" not in evidences
        ):
            rag_ev = evidences.get("rag_evidence", {})
            if isinstance(rag_ev, dict) and not rag_ev.get("error"):
                rag_summary = rag_ev.get("summary", "")
                final_answer = (
                    f"### Oficialía de Gobernanza de Datos y Normativa Banamex\n\n"
                    f"{rag_summary}\n\n"
                    f"---\n"
                    f"*Dictamen emitido por el Oficial de Normativa y Gobernanza Banamex. "
                    f"Información respaldada por la documentación oficial indexada en Pinecone.*"
                )
                return {
                    "final_answer": final_answer,
                    "chart_path": None,
                    "next_agent": "FINISH",
                    "plan": "Pass-through certificado de síntesis normativa de rag_agent.",
                    "messages": [AIMessage(content=final_answer, name="coordinator")],
                }

        # Síntesis final (con toda la evidencia acumulada: SQL, RAG y/o Chart)
        final_answer = synthesize_response(
            question,
            evidences,
            messages,
            user_role=user_role,
            cliente_id=cliente_id,
        )
        return {
            "final_answer": final_answer,
            "chart_path": state.get("chart_path"),
            "next_agent": "FINISH",
            "plan": "Síntesis oficial finalizada por el Data Steward.",
            "messages": [AIMessage(content=final_answer, name="coordinator")],
        }

    # --------------------------------------------------------------------------
    # INICIO DE TURNO (Consulta nueva del usuario)
    # --------------------------------------------------------------------------
    decision = route_or_respond(
        question,
        messages,
        evidences={},
        user_role=user_role,
        cliente_id=cliente_id,
    )

    if decision["can_answer_directly"]:
        answer = decision["direct_answer"]
        return {
            "plan": decision["plan"],
            "next_agent": "FINISH",
            "final_answer": answer,
            "spoke_instruction": None,
            "chart_path": None,
            "step_count": 0,
            "evidences": {},
            "messages": [AIMessage(content=answer, name="coordinator")],
        }

    return {
        "plan": decision["plan"],
        "next_agent": decision["next_agent"],
        "spoke_instruction": decision["spoke_instruction"],
        "chart_path": None,
        "step_count": 1,
        "evidences": {},
        "final_answer": None,
    }
