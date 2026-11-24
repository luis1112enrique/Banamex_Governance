"""
tools/chart_tool.py

Herramienta determinista (Tool + Nodo LangGraph) para generación de gráficas
financieras corporativas de Grupo Financiero Banamex con Matplotlib.

PATRÓN ARQUITECTÓNICO:
- Cero LLM intermedio y cero `exec()` inseguro: recibe una especificación JSON
  estructurada desde el Coordinador (basada en evidencia SQL verificada) y
  dibuja la gráfica de forma 100% determinista.
- Patrón Claim Check: guarda la imagen PNG en `outputs/charts/` y retorna
  únicamente la ruta ligera (`chart_path`) al `AgentState` para no inflar
  los checkpoints de memoria.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # Backend sin interfaz gráfica (seguro en hilos y servidores)
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langsmith import traceable

CHARTS_DIR = Path(__file__).parent.parent / "outputs" / "charts"

# Paleta corporativa inspirada en Grupo Financiero Banamex
BANAMEX_PALETTE = [
    "#003366",  # Azul Marino Corporativo
    "#D9001B",  # Rojo Banamex
    "#0072CE",  # Azul Institucional
    "#009688",  # Verde Azulado
    "#F59E0B",  # Ámbar
    "#6B7280",  # Gris Neutro
]


def _slugify(text: str, max_len: int = 36) -> str:
    """Convierte un título en un nombre de archivo seguro."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "grafica").lower()).strip("_")
    return (cleaned[:max_len] or "grafica").strip("_")


def _format_human_number(val: float, prefix: str = "") -> str:
    """Formatea números grandes de manera compacta y legible (K, M, B)."""
    abs_val = abs(val)
    if abs_val >= 1_000_000_000:
        return f"{prefix}{val / 1_000_000_000:,.2f}B"
    if abs_val >= 1_000_000:
        return f"{prefix}{val / 1_000_000:,.2f}M"
    if abs_val >= 10_000:
        return f"{prefix}{val / 1_000:,.1f}K"
    if float(val).is_integer():
        return f"{prefix}{int(val):,}"
    return f"{prefix}{val:,.2f}"


def _extract_json_spec(raw_text: str) -> Dict[str, Any]:
    """Extrae y parsea el objeto JSON de especificación de gráfica."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    # Intentar parseo directo o extraer primer objeto JSON balanceado
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise ValueError(f"No se pudo interpretar la especificación JSON de la gráfica: {raw_text[:200]}")


def _normalize_series(spec: Dict[str, Any]) -> tuple[List[str], List[Dict[str, Any]]]:
    """Normaliza tanto formato simple (`values`) como multi-serie (`series`)."""
    labels = [str(lbl) for lbl in spec.get("labels", [])]
    series_raw = spec.get("series")

    if series_raw and isinstance(series_raw, list):
        series = []
        for idx, s in enumerate(series_raw):
            name = str(s.get("name", f"Serie {idx + 1}"))
            vals = [float(v) for v in s.get("values", [])]
            if len(vals) != len(labels):
                raise ValueError(
                    f"La serie '{name}' tiene {len(vals)} valores pero hay {len(labels)} etiquetas en el eje X."
                )
            series.append({"name": name, "values": vals})
        return labels, series

    # Fallback: formato plano {"labels": [...], "values": [...]}
    flat_vals = spec.get("values")
    if flat_vals and isinstance(flat_vals, list):
        vals = [float(v) for v in flat_vals]
        if len(vals) != len(labels):
            raise ValueError(
                f"Se recibieron {len(vals)} valores pero {len(labels)} etiquetas."
            )
        series_name = str(spec.get("y_label") or spec.get("series_name") or "Valor")
        return labels, [{"name": series_name, "values": vals}]

    raise ValueError("La especificación de la gráfica no incluye 'series' ni 'values'.")


MONTH_NAMES_ES = {
    "01": "Ene", "02": "Feb", "03": "Mar", "04": "Abr",
    "05": "May", "06": "Jun", "07": "Jul", "08": "Ago",
    "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dic"
}


def _format_date_label(val: Any) -> str:
    """Convierte fechas numéricas tipo YYYYMMDD o YYYYMM a etiquetas legibles en español."""
    s = str(val).strip()
    if len(s) == 8 and s.isdigit():
        y, m = s[:4], s[4:6]
        return f"{MONTH_NAMES_ES.get(m, m)} {y}"
    if len(s) == 6 and s.isdigit():
        y, m = s[:4], s[4:6]
        return f"{MONTH_NAMES_ES.get(m, m)} {y}"
    return s


def build_chart_spec_from_sql(
    columns: List[str],
    rows: List[List[Any]],
    question: str = ""
) -> Optional[Dict[str, Any]]:
    """
    Construye determinísticamente la especificación JSON de la gráfica a partir de los datos
    estructurados de PostgreSQL (0 Tokens de LLM consumidos).
    
    Soporta:
    - 2 columnas: [Etiqueta / Fecha, Métrica Numérica]
    - 3 columnas: [Eje X / Fecha, Categoría / Serie, Métrica Numérica] (Pivote automático multi-serie)
    - Detección inteligente de tipo de gráfica según intención del usuario o estructura temporal.
    """
    if not columns or not rows:
        return None

    q_lower = (question or "").lower()

    # 1. Detección inteligente del tipo de gráfica
    if any(w in q_lower for w in ["dona", "donut"]):
        chart_type = "donut"
    elif any(w in q_lower for w in ["pastel", "pie", "proporcion", "proporción", "distribucion", "distribución", "reparto"]):
        chart_type = "pie"
    elif any(w in q_lower for w in ["horizontal", "ranking", "top", "mayor a menor", "menor a mayor"]):
        chart_type = "horizontal_bar"
    elif any(w in q_lower for w in ["área", "area", "acumulad"]):
        chart_type = "area"
    elif any(w in q_lower for w in ["linea", "línea", "line", "crecimiento", "evolucion", "evolución", "mensual", "tendencia", "tiempo", "histórico"]):
        chart_type = "line"
    elif any(w in q_lower for w in ["barra", "barras", "bar"]):
        chart_type = "bar"
    else:
        # Default según estructura de datos: si col 0 es temporal -> line; si no -> bar
        first_col = str(columns[0]).lower()
        if any(d in first_col for d in ["fec", "mes", "fecha", "vintage", "mob", "corte"]):
            chart_type = "line"
        else:
            chart_type = "bar"

    # Detección de prefijo monetario
    val_prefix = "$" if any(w in " ".join(columns).lower() for w in ["saldo", "monto", "importe", "pesos", "mxn", "amt", "revenue", "expense"]) else ""

    # Caso 1: 3 Columnas -> [Dimensión X (e.g. Fecha), Categoría Serie (e.g. Producto), Valor (e.g. Saldo)]
    if len(columns) == 3:
        x_col, cat_col, val_col = columns
        labels = []
        for r in rows:
            fmt_lbl = _format_date_label(r[0])
            if fmt_lbl not in labels:
                labels.append(fmt_lbl)

        categories = []
        for r in rows:
            c = str(r[1])
            if c not in categories:
                categories.append(c)

        val_map = {}
        for r in rows:
            fmt_lbl = _format_date_label(r[0])
            cat = str(r[1])
            try:
                val = float(r[2]) if r[2] is not None else 0.0
            except (ValueError, TypeError):
                val = 0.0
            val_map[(fmt_lbl, cat)] = val

        series = []
        for cat in categories:
            series.append({
                "name": cat,
                "values": [val_map.get((lbl, cat), 0.0) for lbl in labels]
            })

        if chart_type == "bar" and len(series) > 1:
            chart_type = "grouped_bar"

        return {
            "chart_type": chart_type,
            "title": f"Análisis de {val_col.replace('_', ' ').title()} por {x_col.replace('_', ' ').title()}",
            "x_label": x_col.replace("_", " ").title(),
            "y_label": val_col.replace("_", " ").title(),
            "value_prefix": val_prefix,
            "labels": labels,
            "series": series
        }

    # Caso 2: 2 Columnas -> [Etiqueta / Dimensión X, Métrica Y]
    elif len(columns) == 2:
        lbl_col, val_col = columns
        labels = [_format_date_label(r[0]) for r in rows]
        vals = []
        for r in rows:
            try:
                vals.append(float(r[1]) if r[1] is not None else 0.0)
            except (ValueError, TypeError):
                vals.append(0.0)

        # Si el usuario pidió pastel/dona o es proporción, validamos que no tenga demasiadas rebanadas
        if chart_type in ("pie", "donut") and len(labels) > 7:
            chart_type = "bar"

        return {
            "chart_type": chart_type,
            "title": f"Distribución de {val_col.replace('_', ' ').title()} por {lbl_col.replace('_', ' ').title()}",
            "x_label": lbl_col.replace("_", " ").title(),
            "y_label": val_col.replace("_", " ").title(),
            "value_prefix": val_prefix,
            "labels": labels,
            "series": [{"name": val_col.replace("_", " ").title(), "values": vals}]
        }

    return None


def render_chart_from_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Renderiza la gráfica en disco usando Matplotlib y retorna metadatos + ruta PNG."""
    chart_type = str(spec.get("chart_type", "line")).lower().strip()
    if chart_type not in ("line", "bar", "grouped_bar", "pie", "donut", "horizontal_bar", "area"):
        chart_type = "line"

    title = str(spec.get("title") or "Análisis Financiero — Grupo Financiero Banamex")
    x_label = str(spec.get("x_label") or "")
    y_label = str(spec.get("y_label") or "")
    value_prefix = str(spec.get("value_prefix") or "")

    # Auto-detectar prefijo de moneda si y_label menciona MXN, saldo o monto
    if not value_prefix and any(w in y_label.lower() for w in ("mxn", "peso", "saldo", "importe", "monto", "$")):
        value_prefix = "$"

    labels, series = _normalize_series(spec)
    if not labels or not series:
        raise ValueError("No hay datos suficientes para generar la gráfica.")

    # Si piden 'bar' con múltiples series, usar 'grouped_bar' automáticamente
    if chart_type == "bar" and len(series) > 1:
        chart_type = "grouped_bar"

    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"chart_{timestamp}_{_slugify(title)}.png"
    output_path = (CHARTS_DIR / filename).resolve()

    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "axes.edgecolor": "#CCCCCC",
        "axes.linewidth": 0.8,
    })

    fig, ax = plt.subplots(figsize=(10.5, 5.8), dpi=150)
    fig.patch.set_facecolor("#FFFFFF")
    ax.set_facecolor("#FAFBFC")

    if chart_type == "line":
        x_pos = list(range(len(labels)))
        for idx, s in enumerate(series):
            color = BANAMEX_PALETTE[idx % len(BANAMEX_PALETTE)]
            vals = s["values"]
            ax.plot(
                x_pos,
                vals,
                marker="o",
                linewidth=2.5,
                markersize=7,
                color=color,
                label=s["name"],
            )
            # Anotar valores sobre los puntos si no satura visualmente
            if len(labels) <= 12 and len(series) <= 3:
                for x, y in zip(x_pos, vals):
                    ax.annotate(
                        _format_human_number(y, value_prefix),
                        (x, y),
                        textcoords="offset points",
                        xytext=(0, 8),
                        ha="center",
                        fontsize=8.5,
                        fontweight="bold",
                        color=color,
                    )
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=20 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
        ax.grid(True, linestyle="--", alpha=0.45, color="#B0BEC5")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: _format_human_number(x, value_prefix)))
        if len(series) > 1:
            ax.legend(frameon=True, facecolor="#FFFFFF", edgecolor="#E0E0E0")

    elif chart_type == "bar":
        vals = series[0]["values"]
        x_pos = list(range(len(labels)))
        colors = [BANAMEX_PALETTE[i % len(BANAMEX_PALETTE)] for i in range(len(labels))] if len(labels) <= 6 else BANAMEX_PALETTE[0]
        bars = ax.bar(x_pos, vals, color=colors, width=0.58, edgecolor="#FFFFFF", linewidth=0.8)
        for bar, val in zip(bars, vals):
            ax.annotate(
                _format_human_number(val, value_prefix),
                (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                textcoords="offset points",
                xytext=(0, 5),
                ha="center",
                fontsize=9,
                fontweight="bold",
                color="#1F2937",
            )
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=20 if len(labels) > 5 else 0, ha="right" if len(labels) > 5 else "center")
        ax.grid(True, axis="y", linestyle="--", alpha=0.45, color="#B0BEC5")
        ax.set_axisbelow(True)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: _format_human_number(x, value_prefix)))

    elif chart_type == "grouped_bar":
        n_series = len(series)
        x_base = list(range(len(labels)))
        total_width = 0.78
        bar_w = total_width / n_series
        for idx, s in enumerate(series):
            color = BANAMEX_PALETTE[idx % len(BANAMEX_PALETTE)]
            offset = (idx - (n_series - 1) / 2) * bar_w
            positions = [x + offset for x in x_base]
            bars = ax.bar(positions, s["values"], width=bar_w * 0.92, label=s["name"], color=color)
            if len(labels) * n_series <= 18:
                for bar, val in zip(bars, s["values"]):
                    ax.annotate(
                        _format_human_number(val, value_prefix),
                        (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                        textcoords="offset points",
                        xytext=(0, 4),
                        ha="center",
                        fontsize=7.5,
                        color="#1F2937",
                    )
        ax.set_xticks(x_base)
        ax.set_xticklabels(labels, rotation=20 if len(labels) > 5 else 0, ha="right" if len(labels) > 5 else "center")
        ax.grid(True, axis="y", linestyle="--", alpha=0.45, color="#B0BEC5")
        ax.set_axisbelow(True)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: _format_human_number(x, value_prefix)))
        ax.legend(frameon=True, facecolor="#FFFFFF", edgecolor="#E0E0E0")

    elif chart_type == "pie":
        vals = series[0]["values"]
        colors = [BANAMEX_PALETTE[i % len(BANAMEX_PALETTE)] for i in range(len(labels))]
        wedges, texts, autotexts = ax.pie(
            vals,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            colors=colors,
            pctdistance=0.75,
            wedgeprops={"edgecolor": "#FFFFFF", "linewidth": 1.5},
        )
        for at in autotexts:
            at.set_color("#FFFFFF")
            at.set_fontweight("bold")
            at.set_fontsize(9.5)
        ax.axis("equal")

    elif chart_type == "donut":
        vals = series[0]["values"]
        colors = [BANAMEX_PALETTE[i % len(BANAMEX_PALETTE)] for i in range(len(labels))]
        wedges, texts, autotexts = ax.pie(
            vals,
            labels=labels,
            autopct="%1.1f%%",
            startangle=140,
            colors=colors,
            pctdistance=0.78,
            wedgeprops={"width": 0.42, "edgecolor": "#FFFFFF", "linewidth": 1.5},
        )
        for at in autotexts:
            at.set_color("#FFFFFF")
            at.set_fontweight("bold")
            at.set_fontsize(9.5)
        total_val = sum(vals)
        ax.text(0, 0, f"Total\n{_format_human_number(total_val, value_prefix)}", ha="center", va="center", fontsize=11, fontweight="bold", color="#003366")
        ax.axis("equal")

    elif chart_type == "horizontal_bar":
        vals = series[0]["values"]
        y_pos = list(range(len(labels)))
        colors = [BANAMEX_PALETTE[i % len(BANAMEX_PALETTE)] for i in range(len(labels))] if len(labels) <= 6 else BANAMEX_PALETTE[0]
        bars = ax.barh(y_pos, vals, color=colors, height=0.58, edgecolor="#FFFFFF", linewidth=0.8)
        for bar, val in zip(bars, vals):
            ax.annotate(
                _format_human_number(val, value_prefix),
                (bar.get_width(), bar.get_y() + bar.get_height() / 2),
                textcoords="offset points",
                xytext=(6, 0),
                va="center",
                fontsize=9,
                fontweight="bold",
                color="#1F2937",
            )
        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels, fontsize=9.5)
        ax.grid(True, axis="x", linestyle="--", alpha=0.45, color="#B0BEC5")
        ax.set_axisbelow(True)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: _format_human_number(x, value_prefix)))

    elif chart_type == "area":
        x_pos = list(range(len(labels)))
        for idx, s in enumerate(series):
            color = BANAMEX_PALETTE[idx % len(BANAMEX_PALETTE)]
            vals = s["values"]
            ax.fill_between(x_pos, vals, alpha=0.25, color=color)
            ax.plot(x_pos, vals, marker="o", linewidth=2.5, markersize=6, color=color, label=s["name"])
        ax.set_xticks(x_pos)
        ax.set_xticklabels(labels, rotation=20 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
        ax.grid(True, linestyle="--", alpha=0.45, color="#B0BEC5")
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: _format_human_number(x, value_prefix)))
        if len(series) > 1:
            ax.legend(frameon=True, facecolor="#FFFFFF", edgecolor="#E0E0E0")

    # Títulos y ejes
    ax.set_title(title, fontsize=13.5, fontweight="bold", color="#003366", pad=16)
    if chart_type not in ("pie", "donut"):
        if x_label:
            ax.set_xlabel(x_label, fontsize=10.5, fontweight="bold", color="#374151", labelpad=10)
        if y_label:
            ax.set_ylabel(y_label, fontsize=10.5, fontweight="bold", color="#374151", labelpad=10)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        # Margen superior proporcional al rango de datos para que las etiquetas no se corten
        ymin, ymax = ax.get_ylim()
        span = (ymax - ymin) if (ymax - ymin) > 0 else (abs(ymax) * 0.1 or 1.0)
        ax.set_ylim(ymin, ymax + span * 0.18)

    fig.text(
        0.99,
        0.01,
        "Fuente: Grupo Financiero Banamex — Plataforma Data Steward",
        ha="right",
        va="bottom",
        fontsize=8,
        color="#6B7280",
        style="italic",
    )

    plt.tight_layout(rect=[0, 0.03, 1, 1])
    fig.savefig(output_path, dpi=150)
    plt.close(fig)

    return {
        "chart_path": str(output_path),
        "title": title,
        "chart_type": chart_type,
        "labels_count": len(labels),
        "series_count": len(series),
    }


@tool
def generate_banamex_chart(chart_spec_json: str) -> str:
    """Genera una gráfica PNG corporativa de Banamex a partir de un JSON con:
    chart_type ('line'|'bar'|'grouped_bar'|'pie'), title, x_label, y_label,
    labels (lista de strings), y series ([{'name': str, 'values': [float]}]).
    Devuelve la ruta absoluta del archivo PNG guardado en outputs/charts/."""
    spec = _extract_json_spec(chart_spec_json)
    info = render_chart_from_spec(spec)
    return (
        f"Gráfica generada exitosamente.\n"
        f"- Título: {info['title']}\n"
        f"- Tipo: {info['chart_type']}\n"
        f"- Archivo PNG: {info['chart_path']}"
    )


@traceable(run_type="tool", name="matplotlib_chart_tool")
def run_chart_tool(instruction: str) -> dict:
    """Ejecuta la tool graficadora y retorna un dict estructurado para `evidences`."""
    try:
        spec = _extract_json_spec(instruction)
        info = render_chart_from_spec(spec)
        summary = (
            f"Gráfica corporativa generada con éxito (Matplotlib):\n"
            f"- Título: {info['title']}\n"
            f"- Tipo de gráfico: {info['chart_type']}\n"
            f"- Puntos/Categorías graficadas: {info['labels_count']} ({info['series_count']} serie(s))\n"
            f"- Ruta del archivo PNG: {info['chart_path']}"
        )
        return {
            "chart_path": info["chart_path"],
            "title": info["title"],
            "chart_type": info["chart_type"],
            "summary": summary,
            "error": None,
        }
    except Exception as e:
        error_msg = f"Error al generar la gráfica: {str(e)}"
        return {
            "chart_path": None,
            "title": "Error de graficación",
            "chart_type": "N/A",
            "summary": error_msg,
            "error": str(e),
        }


def chart_tool_node(state: dict) -> dict:
    """
    Nodo determinista de LangGraph para la Tool Graficadora.
    Lee la especificación JSON en `spoke_instruction`, guarda el PNG en disco
    y deposita la ruta en `chart_path` + `evidences['chart_evidence']`.
    """
    instruction = state.get("spoke_instruction") or ""
    result = run_chart_tool(instruction)

    msg_content = f"### [EVIDENCIA GRÁFICA - CHART TOOL]\n{result['summary']}"

    return {
        "chart_path": result.get("chart_path"),
        "evidences": {"chart_evidence": result},
        "messages": [AIMessage(content=msg_content, name="chart_tool")],
    }
