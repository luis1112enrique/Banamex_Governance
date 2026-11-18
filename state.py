"""
state.py

Definición del estado global (AgentState) para la arquitectura Hub-and-Spoke de Banamex.
Mantiene el historial de mensajes, el plan del supervisor, la instrucción enriquecida y las evidencias.
"""

from typing import Annotated, Sequence, TypedDict, Dict, Any, Optional, List
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


def merge_evidences(existing: Dict[str, Any], new_evidence: Dict[str, Any]) -> Dict[str, Any]:
    """Combina evidencias recopiladas por los distintos agentes spoke.
    Si se pasa un diccionario vacío explícito ({}), resetea la evidencia para un nuevo turno."""
    if new_evidence == {}:
        return {}
    updated = dict(existing or {})
    if new_evidence:
        updated.update(new_evidence)
    return updated


def merge_documents(existing: Dict[str, Any], new_docs: Dict[str, Any]) -> Dict[str, Any]:
    """Acumula los documentos cargados en el hilo; un archivo con el mismo nombre reemplaza al anterior."""
    updated = dict(existing or {})
    updated.update(new_docs or {})
    return updated


class AgentState(TypedDict):
    # Historial de conversación manejado automáticamente por LangGraph
    messages: Annotated[Sequence[BaseMessage], add_messages]
    
    # Pregunta actual del usuario
    question: str

    # Rol del usuario autenticado ('universal' = acceso total a SQL, 'cliente' = restringido a su propio num_cte)
    user_role: Optional[str]

    # Identificador único del cliente en PostgreSQL (dim.dim_cliente.num_cte). None cuando user_role == 'universal'.
    cliente_id: Optional[int]
    
    # Razonamiento y plan del Coordinador (Hub)
    plan: Optional[str]
    
    # Siguiente agente a invocar ('sql_agent', 'rag_agent', 'chart_tool' o 'FINISH')
    next_agent: Optional[str]

    # Instrucción técnica contextualizada y enriquecida que el Coordinador le envía al Spoke
    # (Para 'chart_tool', contiene el JSON estructurado con los datos y ejes a graficar)
    spoke_instruction: Optional[str]
    
    # Almacén de evidencias recopiladas por los agentes especializados durante el turno
    evidences: Annotated[Dict[str, Any], merge_evidences]

    # Ruta absoluta al archivo PNG generado por 'chart_tool' (Patrón Claim Check: nunca bytes en el estado)
    chart_path: Optional[str]

    # Contador de saltos (delegaciones) dentro del turno actual para prevenir bucles infinitos en multi-hop
    step_count: Optional[int]

    # Rutas de archivos cargados por el usuario en este turno para OCR (limpiado en cada turno)
    uploaded_files: Optional[List[str]]

    # Texto extraído por el procesador OCR: {nombre_archivo: {"total_pages": int, "pages": [{page, text, method}]}}.
    # No se indexa en Pinecone; el RAG Agent lo usa directamente como contexto. Persiste en el hilo
    uploaded_documents: Annotated[Dict[str, Any], merge_documents]
    
    # Respuesta final formulada y validada por el Data Steward
    final_answer: Optional[str]
