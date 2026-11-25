"""
agents/doc_agent.py

SPOKE MULTIMODAL DE ANÁLISIS Y CLASIFICACIÓN DE DOCUMENTOS (DOC AGENT).
Inspecciona expedientes de clientes (imágenes, PDFs, documentos de texto) utilizando
las capacidades de visión y análisis multimodal nativas de Google Gemini.

RESPONSABILIDAD ÚNICA:
- "¿Qué documentos tiene el cliente y qué datos contienen?"
- Clasifica cada archivo en categorías bancarias (Identificación Oficial, Comprobante de Domicilio,
  Comprobante de Ingresos, etc.), extrae titular, vigencia, fecha, estado de legibilidad y campos clave.
- NO valida políticas de crédito ni contratos (eso lo hace el RAG Agent y el Coordinador).
- NO requiere Tesseract ni binarios externos de OCR en el sistema operativo.
"""

import os
import json
import base64
import mimetypes
from pathlib import Path
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from langsmith import traceable
from pypdf import PdfReader

from agents.llm import get_gemini_llm

load_dotenv(Path(__file__).parent.parent / ".env")

DOC_MODEL = os.environ.get("DOC_MODEL", "gemini-3.6-flash")

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_PDF_EXTENSIONS = {".pdf"}
SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md", ".csv"}


def get_doc_llm():
    """Retorna Gemini configurado para análisis documental estructurado."""
    return get_gemini_llm(model=DOC_MODEL, temperature=0.1)


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


DOC_SYSTEM_PROMPT = """Eres el Analista Multimodal de Documentos de Grupo Financiero Banamex (Doc Agent Spoke).
Tu misión es inspeccionar visual y textualmente los archivos cargados por el cliente, clasificarlos con rigor bancario y extraer sus datos clave.

CATEGORÍAS DE DOCUMENTOS BANCARIOS:
1. 'IDENTIFICACION_OFICIAL': Credencial para Votar (INE/IFE), Pasaporte mexicano, Cédula Profesional o Cartilla Militar.
2. 'COMPROBANTE_DOMICILIO': Recibo de luz (CFE), agua, predial, teléfono fijo/móvil o gas natural.
3. 'COMPROBANTE_INGRESOS': Recibo de nómina con CFDI/timbrado, Estado de Cuenta Bancario, Declaración Anual/SAT o Carta Laboral.
4. 'CONTRATO_U_OTRO': Contratos externos, solicitudes, firmas o documentos no catalogados en las categorías anteriores.

REGLAS DE EXTRACCIÓN Y RIGOR:
- Determina con exactitud el Titular que aparece en el documento.
- Identifica la Fecha de emisión, vencimiento o periodo del documento.
- Evalúa el Estado del documento: 'VIGENTE', 'VENCIDO', 'ILEGIBLE' o 'NO_ESPECIFICADO'.
- Extrae campos clave relevantes para cotejo financiero (RFC, CURP, Clave de Elector, Domicilio completo, Saldo/Ingreso promedio o sueldo neto si aplica).
- Prohibido inventar datos; si un dato no es legible o no aparece, indícalo como 'NO_LEGIBLE' o 'NO_ESPECIFICADO'.

FORMATO DE RESPUESTA OBLIGATORIO:
Responde ÚNICAMENTE un objeto JSON válido con la siguiente estructura (sin bloques de código markdown antes ni después):
{
  "documents": [
    {
      "filename": "nombre_archivo.ext",
      "tipo_documento": "IDENTIFICACION_OFICIAL | COMPROBANTE_DOMICILIO | COMPROBANTE_INGRESOS | CONTRATO_U_OTRO",
      "subtipo": "Ej. Credencial para Votar INE, Recibo de Luz CFE, etc.",
      "titular": "Nombre completo encontrado o 'NO_ESPECIFICADO'",
      "vigencia_o_fecha": "Fecha observada o vigencia (ej. 2024-2034, Periodo Sep-Oct 2026)",
      "estado": "VIGENTE | VENCIDO | ILEGIBLE | NO_ESPECIFICADO",
      "campos_clave": {
        "clave_identificacion": "...",
        "domicilio": "...",
        "monto_o_ingreso": "..."
      },
      "observaciones": "Comentario técnico sobre legibilidad, nitidez o datos incompletos"
    }
  ],
  "summary": "Resumen ejecutivo en 1 o 2 párrafos enumerando los documentos analizados y sus hallazgos principales."
}
"""


def _load_file_payload(file_path: Path) -> List[Dict[str, Any]]:
    """Convierte un archivo en bloques de contenido para HumanMessage de Gemini."""
    ext = file_path.suffix.lower()

    if ext in SUPPORTED_IMAGE_EXTENSIONS:
        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "image/jpeg"
        raw_bytes = file_path.read_bytes()
        b64 = base64.b64encode(raw_bytes).decode("utf-8")
        return [
            {"type": "text", "text": f"--- ARCHIVO DE IMAGEN: {file_path.name} ---"},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        ]

    if ext in SUPPORTED_PDF_EXTENSIONS:
        try:
            reader = PdfReader(file_path)
            pages_text = []
            for idx, p in enumerate(reader.pages):
                txt = (p.extract_text() or "").strip()
                if txt:
                    pages_text.append(f"[Página {idx + 1}]:\n{txt}")
            combined = "\n\n".join(pages_text) if pages_text else "Documento PDF escaneado sin capa de texto nativo."
            return [
                {"type": "text", "text": f"--- DOCUMENTO PDF: {file_path.name} (Total páginas: {len(reader.pages)}) ---\n{combined}"}
            ]
        except Exception as e:
            return [{"type": "text", "text": f"--- ARCHIVO PDF {file_path.name} (Error al leer): {str(e)} ---"}]

    if ext in SUPPORTED_TEXT_EXTENSIONS:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return [
            {"type": "text", "text": f"--- DOCUMENTO DE TEXTO: {file_path.name} ---\n{content}"}
        ]

    return [{"type": "text", "text": f"--- ARCHIVO NO SOPORTADO: {file_path.name} (Extensión: {ext}) ---"}]


@traceable(run_type="chain", name="doc_analyst_spoke")
def run_doc_agent(file_paths: List[str], instruction: Optional[str] = None) -> Dict[str, Any]:
    """
    Ejecuta el análisis multimodal de documentos con Gemini:
    1. Carga imágenes y archivos de forma nativa.
    2. Clasifica tipo de documento (Identificación, Domicilio, Ingresos).
    3. Extrae titular, fechas de vigencia y campos clave estructurados.
    """
    if not file_paths:
        return {
            "documents": [],
            "summary": "No se proporcionaron archivos para análisis en este turno.",
            "error": "NO_FILES_PROVIDED",
        }

    valid_paths: List[Path] = []
    missing_files: List[str] = []

    for raw in file_paths:
        p = Path(raw).expanduser().resolve()
        if p.is_file():
            valid_paths.append(p)
        else:
            missing_files.append(raw)

    if not valid_paths:
        return {
            "documents": [],
            "summary": f"No se encontraron en disco los archivos especificados: {', '.join(missing_files)}",
            "error": "FILES_NOT_FOUND",
        }

    # Construir el payload multimodal
    content_blocks: List[Dict[str, Any]] = [
        {"type": "text", "text": f"INSTRUCCIÓN DEL COORDINADOR:\n{instruction or 'Clasifica y extrae los datos de los siguientes documentos bancarios.'}\n"}
    ]

    for p in valid_paths:
        content_blocks.extend(_load_file_payload(p))

    try:
        llm = get_doc_llm()
        response = llm.invoke([
            SystemMessage(content=DOC_SYSTEM_PROMPT),
            HumanMessage(content=content_blocks),
        ])

        raw_text = extract_content_text(response).strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[-1]
            if raw_text.endswith("```"):
                raw_text = raw_text.rsplit("```", 1)[0]
        raw_text = raw_text.strip()

        parsed = json.loads(raw_text)
        docs = parsed.get("documents", [])
        summary = parsed.get("summary", "Documentos analizados correctamente.")

        # Si el modelo no armó un summary descriptivo, lo generamos de respaldo
        if not summary or summary.strip() == "":
            lines = [f"Se completó la inspección de {len(docs)} documento(s):"]
            for d in docs:
                lines.append(f"- **{d.get('filename')}**: {d.get('subtipo')} (Titular: {d.get('titular')}, Estado: {d.get('estado')})")
            summary = "\n".join(lines)

        return {
            "documents": docs,
            "summary": summary,
            "missing_files": missing_files,
            "error": None,
        }

    except Exception as e:
        error_msg = f"Error en Doc Agent al analizar documentos: {str(e)}"
        return {
            "documents": [],
            "summary": error_msg,
            "missing_files": missing_files,
            "error": str(e),
        }


def doc_agent_node(state: dict) -> dict:
    """
    Nodo de LangGraph para el Spoke Analista Multimodal de Documentos.
    Lee 'uploaded_files' del estado compartido y entrega el inventario estructurado en 'doc_evidence'.
    """
    files = state.get("uploaded_files") or []
    instruction = state.get("spoke_instruction") or state.get("question", "")

    result = run_doc_agent(files, instruction=instruction)
    msg_content = f"### [INVENTARIO Y CLASIFICACIÓN DE DOCUMENTOS - DOC AGENT]\n{result['summary']}"

    return {
        "evidences": {"doc_evidence": result},
        "messages": [AIMessage(content=msg_content, name="doc_agent")],
    }
