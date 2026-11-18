"""
workflow.py

Orquestación del Grafo Multi-Agente (Hub-and-Spoke Multi-Hop) con LangGraph.
Define el flujo cíclico con soporte para múltiples saltos:
   [START] -> [Coordinador Hub]
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
   [SQL Spoke] [RAG Tool] [Chart Tool]
        │         │         │
        └─────────┼─────────┘
                  ▼
          [Coordinador Hub] -> (Siguiente Spoke o Sintetizar y FINISH) -> [END]

Ejemplo Multi-Hop:
  Coordinador -> sql_agent -> Coordinador -> chart_tool -> Coordinador (Síntesis) -> END

Incluye memoria de checkpointing (MemorySaver) para el historial de la sesión.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState
from agents.coordinator import coordinator_node
from agents.sql_agent import sql_agent_node
from agents.rag_agent import rag_agent_node
from tools.chart_tool import chart_tool_node
from processing.ocr_processor import ocr_processor_node


def route_entry(state: AgentState) -> str:
    """Si el usuario cargó archivos en este turno, primero pasan por el procesador OCR."""
    return "ocr_processor" if state.get("uploaded_files") else "coordinator"


def route_after_ocr(state: AgentState) -> str:
    """Si el OCR extrajo texto con éxito, el RAG analiza; si todo falló, el Coordinador reporta el error."""
    ocr_ev = (state.get("evidences") or {}).get("ocr_evidence") or {}
    return "rag_agent" if ocr_ev.get("extracted_sources") else "coordinator"


def route_next_spoke(state: AgentState) -> str:
    """Función de enrutamiento condicional basada en la decisión del Coordinador."""
    next_agent = state.get("next_agent", "FINISH")
    if next_agent in ["sql_agent", "rag_agent", "chart_tool"]:
        return next_agent
    return END


def create_hub_spoke_graph():
    """Construye y compila el flujo StateGraph de LangGraph."""
    graph = StateGraph(AgentState)

    # 1. Registrar nodos activos
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("sql_agent", sql_agent_node)
    graph.add_node("rag_agent", rag_agent_node)
    graph.add_node("chart_tool", chart_tool_node)
    graph.add_node("ocr_processor", ocr_processor_node)

    # 2. Conectar inicio:
    #    - Si hay archivos cargados: START -> ocr_processor -> rag_agent -> coordinator
    #    - Turno normal: START -> coordinator -> spokes -> coordinator
    graph.add_conditional_edges(
        START,
        route_entry,
        {
            "ocr_processor": "ocr_processor",
            "coordinator": "coordinator",
        }
    )
    graph.add_conditional_edges(
        "ocr_processor",
        route_after_ocr,
        {
            "rag_agent": "rag_agent",
            "coordinator": "coordinator",
        }
    )

    # 3. Ruteo condicional desde el Coordinador hacia los Spokes o hacia END
    graph.add_conditional_edges(
        "coordinator",
        route_next_spoke,
        {
            "sql_agent": "sql_agent",
            "rag_agent": "rag_agent",
            "chart_tool": "chart_tool",
            END: END,
        }
    )

    # 4. Todos los Spokes devuelven el control al Coordinador (para siguiente salto o síntesis)
    graph.add_edge("sql_agent", "coordinator")
    graph.add_edge("rag_agent", "coordinator")
    graph.add_edge("chart_tool", "coordinator")

    # 5. Compilar con memoria local en memoria (checkpointing)
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)
    return app


# Instancia compilada lista para usar
multi_agent_app = create_hub_spoke_graph()
