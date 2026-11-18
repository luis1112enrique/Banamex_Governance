"""
agents/rag_agent.py

SPOKE INTELIGENTE DE GOBERNANZA DOCUMENTAL Y NORMATIVA BANAMEX (RAG AGENT).
Especializado en análisis normativo, contractual, comisiones y políticas oficiales.

ARQUITECTURA (AISLAMIENTO DE CONTEXTO):
1. Recuperación Determinista: Consulta el índice 'banamex-governance' en Pinecone usando bge-large (k=6)
   a través del retriever modular en tools/rag_tool.py.
2. Síntesis Especializada: Utiliza Google Gemini (gemini-3.5-flash / gemini-3.6-flash) internamente
   para digerir miles de tokens de cláusulas legales y redactar un informe ejecutivo estructurado (200-300 palabras).
3. Aislamiento de Memoria: Solo entrega la síntesis final al Coordinador a través del canal de estado
   (state['evidences']['rag_evidence']['summary']), evitando sobrecargar el contexto del Hub.
"""

import os
from pathlib import Path
from typing import Dict, Any, List
from dotenv import load_dotenv

from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from langsmith import traceable

from agents.llm import get_gemini_llm
from tools.rag_tool import get_retriever

load_dotenv(Path(__file__).parent.parent / ".env")

RAG_MODEL = os.environ.get("RAG_MODEL", "gemini-3.5-flash")


def get_rag_llm():
    """Retorna la instancia de Gemini para el spoke de RAG con baja temperatura (rigor contractual)."""
    return get_gemini_llm(model=RAG_MODEL, temperature=0.1)


def extract_content_text(response) -> str:
    """Extrae texto de una respuesta de LangChain sin importar si es str o lista de bloques."""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        text_parts = [
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        ]
        return "".join(text_parts)
    return str(content)


RAG_SYSTEM_PROMPT = """Eres el Especialista en Normativa, Contratos y Gobernanza de Grupo Financiero Banamex (RAG Agent Spoke).
Tu misión es analizar la documentación oficial bancaria recuperada de Pinecone y responder a la consulta con máxima precisión legal, financiera y técnica.

DIRECTIVAS ESTRICTAS DE CUMPLIMIENTO BANCARIO:
1. FIDELIDAD CONTRACTUAL ABSOLUTA:
   - Basa tus respuestas EXCLUSIVAMENTE en los fragmentos de documentos oficiales provistos en la sección EVIDENCIA DOCUMENTAL.
   - Jamás inventes comisiones, cuotas anuales, porcentajes de CAT, tasas de interés, plazos ni requisitos que no figuren en la evidencia.
   - Si la evidencia no menciona un dato específico solicitado, indícalo con transparencia: "La documentación oficial consultada no especifica [dato]".

2. FORMATO EJECUTIVO Y ESTRUCTURADO:
   Cuando expliques un producto, servicio o trámite, organiza la respuesta con claridad:
   - **Resumen del Producto / Servicio**: Propósito y características principales.
   - **Costos y Comisiones**: Cuota anual, reposición, no pago, transferencias o manejo de cuenta (especificando si son + IVA).
   - **CAT y Tasas (si aplica)**: Costo Anual Total promedio ponderado, tasa de interés ordinaria y moratoria.
   - **Condiciones y Requisitos**: Requisitos de contratación o condiciones para exentar comisiones (ej. saldo promedio mínimo).
   - **Fundamento Documental**: Cita al final los contratos o folletos de donde se extrajo la información con su página o sección.

3. TONO INSTITUCIONAL:
   - Mantén un tono formal, profesional, objetivo y de soporte bancario institucional ("Oficialía de Normativa Banamex").
"""


@traceable(run_type="chain", name="rag_agent_spoke")
def run_rag_agent(query: str) -> Dict[str, Any]:
    """
    Ejecuta el ciclo de vida del Spoke RAG:
    1. Recuperación de fragmentos en Pinecone (k=6).
    2. Filtrado y estructuración de contexto documental.
    3. Síntesis y análisis normativo con Gemini 3.6 Flash.
    4. Extracción de fuentes citadas para auditoría.
    """
    try:
        retriever = get_retriever(k=6)
        docs = retriever.invoke(query)

        if not docs:
            return {
                "summary": (
                    "No se encontró documentación oficial aplicable en el repositorio normativo de Banamex "
                    f"para la consulta: '{query}'."
                ),
                "retrieved_sources": [],
                "raw_evidence_count": 0,
                "error": None,
            }

        # Formateo de los fragmentos recuperados con sus metadatos
        context_blocks = []
        sources_seen = set()
        sources_list: List[Dict[str, Any]] = []

        for i, d in enumerate(docs, 1):
            source = d.metadata.get("source", "documento_banamex")
            page = d.metadata.get("page")
            tipo = d.metadata.get("tipo_documento", "documento")
            prod = d.metadata.get("producto_o_familia", "general")
            url = d.metadata.get("url_fuente", "https://www.banamex.com")

            source_key = (source, page)
            if source_key not in sources_seen:
                sources_seen.add(source_key)
                sources_list.append({
                    "source": source,
                    "page": page,
                    "tipo": tipo,
                    "producto": prod,
                    "url": url,
                })

            header = f"[FUENTE #{i} | Archivo: {source}"
            if page is not None:
                header += f" (Pág. {page})"
            header += f" | Tipo: {tipo} | Familia: {prod}]"

            context_blocks.append(f"{header}\nURL: {url}\n{d.page_content.strip()}")

        evidence_text = "\n\n---\n\n".join(context_blocks)

        # Invocación de síntesis interna en el spoke (Gemini 3.6 Flash)
        llm = get_rag_llm()
        user_prompt = (
            f"CONSULTA DEL USUARIO / INSTRUCCIÓN:\n{query}\n\n"
            f"EVIDENCIA DOCUMENTAL OFICIAL RECUPERADA DE PINECONE:\n{evidence_text}\n\n"
            f"Elabora el informe normativo oficial conforme a las directivas de cumplimiento."
        )

        response = llm.invoke([
            SystemMessage(content=RAG_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])

        synthesized_report = extract_content_text(response).strip()

        return {
            "summary": synthesized_report,
            "retrieved_sources": sources_list,
            "raw_evidence_count": len(docs),
            "error": None,
        }

    except Exception as e:
        error_msg = f"Error técnico en el RAG Agent Spoke al consultar Pinecone o sintetizar: {str(e)}"
        return {
            "summary": error_msg,
            "retrieved_sources": [],
            "raw_evidence_count": 0,
            "error": str(e),
        }


def rag_agent_node(state: dict) -> dict:
    """
    Nodo de LangGraph para el spoke de documentación y gobernanza (RAG Agent Spoke).
    Ejecuta el análisis aislado y devuelve la síntesis terminada en `evidences['rag_evidence']`.
    """
    instruction = state.get("spoke_instruction") or state.get("question", "")
    result = run_rag_agent(instruction)

    msg_content = f"### [ANÁLISIS DOCUMENTAL Y NORMATIVA BANAMEX]\n{result['summary']}"

    return {
        "evidences": {"rag_evidence": result},
        "messages": [AIMessage(content=msg_content, name="rag_agent")],
    }
