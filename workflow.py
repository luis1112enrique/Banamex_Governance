"""
workflow.py

Orquestación del Grafo Multi-Agente (Hub-and-Spoke Multi-Hop) con LangGraph.
Define el flujo cíclico centrado en el Hub Coordinador:
   [START] -> [Coordinador Hub]
                  │
        ┌─────────┼─────────┬─────────┐
        ▼         ▼         ▼         ▼
   [SQL Spoke] [RAG Spoke] [Plot Tool] [Doc Agent]
        │         │         │         │
        └─────────┴─────────┼─────────┘
                            ▼
                    [Coordinador Hub] -> (Siguiente Spoke o Síntesis y FINISH) -> [END]

Ejemplo Multi-Hop Documental:
  Coordinador -> doc_agent (inventario) -> Coordinador -> rag_agent (requisitos) -> Coordinador (Cruce) -> END

Incluye memoria de checkpointing (MemorySaver) para el historial de la sesión.
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver

from state import AgentState
from agents.coordinator import coordinator_node
from agents.sql_agent import sql_agent_node
from agents.rag_agent import rag_agent_node
from agents.doc_agent import doc_agent_node
from tools.chart_tool import chart_tool_node


def route_next_spoke(state: AgentState) -> str:
    """Función de enrutamiento condicional basada en la decisión del Coordinador."""
    next_agent = state.get("next_agent", "FINISH")
    if next_agent in ["sql_agent", "rag_agent", "chart_tool", "doc_agent"]:
        return next_agent
    return END


def create_hub_spoke_graph():
    """Construye y compila el flujo StateGraph de LangGraph."""
    graph = StateGraph(AgentState)

    # 1. Registrar nodos activos del sistema
    graph.add_node("coordinator", coordinator_node)
    graph.add_node("sql_agent", sql_agent_node)
    graph.add_node("rag_agent", rag_agent_node)
    graph.add_node("chart_tool", chart_tool_node)
    graph.add_node("doc_agent", doc_agent_node)

    # 2. Conectar inicio con el Hub Coordinador
    graph.add_edge(START, "coordinator")

    # 3. Ruteo condicional desde el Coordinador hacia los Spokes o hacia END
    graph.add_conditional_edges(
        "coordinator",
        route_next_spoke,
        {
            "sql_agent": "sql_agent",
            "rag_agent": "rag_agent",
            "chart_tool": "chart_tool",
            "doc_agent": "doc_agent",
            END: END,
        }
    )

    # 4. Todos los Spokes devuelven el control al Coordinador (para siguiente salto o síntesis)
    graph.add_edge("sql_agent", "coordinator")
    graph.add_edge("rag_agent", "coordinator")
    graph.add_edge("chart_tool", "coordinator")
    graph.add_edge("doc_agent", "coordinator")

    # 5. Compilar con memoria local en memoria (checkpointing)
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)
    return app


# Instancia compilada lista para usar
multi_agent_app = create_hub_spoke_graph()
