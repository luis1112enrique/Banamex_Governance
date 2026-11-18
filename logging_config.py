"""
logging_config.py

Formateo visual enriquecido para la trazabilidad del sistema multi-agente de Banamex.
Permite visualizar claramente qué decisiones toma el Hub y qué evidencia genera cada Spoke.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def log_header(title: str):
    console.print()
    console.print(Panel(Text(title, justify="center", style="bold cyan"), border_style="cyan"))


def log_coordinator_decision(plan: str, next_agent: str):
    table = Table(title="Decisión del Coordinador (Hub)", border_style="yellow", show_header=True)
    table.add_column("Métrica", style="bold yellow")
    table.add_column("Detalle", style="white")
    table.add_row("Plan / Razonamiento", plan)
    table.add_row("Próximo Agente Destino", f"[bold green]{next_agent}[/bold green]")
    console.print(table)


def log_sql_execution(query: str, result_summary: str):
    content = f"[bold green]SQL Generado:[/bold green]\n{query}\n\n[bold green]Resultado Obtenido:[/bold green]\n{result_summary}"
    console.print(Panel(content, title="Spoke: SQL Agent (Gemini + PostgreSQL)", border_style="green"))


def log_rag_retrieval(query: str, sources_summary: str):
    content = f"[bold blue]Consulta Normativa:[/bold blue] {query}\n\n[bold blue]Síntesis Normativa y Evidencia Oficial:[/bold blue]\n{sources_summary}"
    console.print(Panel(content, title="Spoke: RAG Agent (Gemini 3.6 Flash + Pinecone)", border_style="blue"))


def log_chart_generation(title: str, chart_type: str, chart_path: str, summary: str):
    content = (
        f"[bold magenta]Título:[/bold magenta] {title}\n"
        f"[bold magenta]Tipo:[/bold magenta] {chart_type}\n"
        f"[bold magenta]Archivo PNG:[/bold magenta] {chart_path}\n\n"
        f"{summary}"
    )
    console.print(Panel(content, title="Spoke: Chart Tool (Matplotlib PNG)", border_style="magenta"))


def log_final_response(answer: str, chart_path: str | None = None):
    console.print()
    body = answer
    if chart_path:
        body += f"\n\n[bold magenta]Gráfica adjunta guardada en:[/bold magenta] {chart_path}"
    console.print(Panel(body, title="Respuesta Final - Data Steward Banamex", border_style="bold bright_green"))


def log_fatal_error(message: str):
    console.print()
    console.print(Panel(message, title="ERROR TERMINAL - Ejecución detenida", border_style="bold red"))
