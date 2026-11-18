"""
agents/ocr_agent.py

Spoke de OCR para documentos cargados por el usuario.
Flujo:  archivo(s) -> extracción de texto por página -> canal 'uploaded_documents' del estado -> RAG Agent
NO escribe en Pinecone: el texto extraído viaja en el estado del grafo y el RAG Agent lo usa
directamente como contexto (junto con lo que recupere del corpus oficial).

Estrategia de extracción (híbrida, para ahorrar llamadas al LLM):
- PDF digital: se usa el texto nativo de pypdf si la página tiene al menos OCR_MIN_CHARS caracteres.
- PDF escaneado / páginas con poco texto / imágenes: OCR multimodal con Google Gemini (OCR_MODEL).
- TXT / MD: lectura directa.
Con OCR_FORCE=true todas las páginas PDF pasan por Gemini.
"""

import os
import io
import base64
import mimetypes
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from pypdf import PdfReader, PdfWriter
from langchain_core.messages import AIMessage, HumanMessage

from agents.llm import get_gemini_llm

load_dotenv(Path(__file__).parent.parent / ".env")

OCR_MODEL = os.environ.get("OCR_MODEL") or os.environ.get("COORDINATOR_MODEL", "gemini-3.5-flash")
OCR_MIN_CHARS = int(os.environ.get("OCR_MIN_CHARS", "80"))
OCR_FORCE = os.environ.get("OCR_FORCE", "false").strip().lower() in ("1", "true", "yes", "si", "sí")
OCR_MAX_WORKERS = 4

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
TEXT_EXTENSIONS = {".txt", ".md"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS

OCR_PROMPT = """Eres un motor de OCR de alta precisión para documentos bancarios de Banamex.
Transcribe TODO el texto visible del documento adjunto, en su idioma original y en orden de lectura.
Reglas:
1. No resumas, no traduzcas, no corrijas ni inventes contenido. Si una palabra es ilegible escribe [ilegible].
2. Conserva montos, porcentajes, fechas, números de cuenta/cláusula y nombres EXACTAMENTE como aparecen.
3. Representa las tablas en formato Markdown y conserva los encabezados y la jerarquía de secciones.
4. Responde únicamente con la transcripción, sin comentarios antes ni después.
5. Si el documento no contiene texto, responde exactamente: [SIN TEXTO]
"""


def get_ocr_llm():
    """Retorna Gemini. Lanza ConfigError (error terminal) si falta GOOGLE_API_KEY."""
    return get_gemini_llm(OCR_MODEL)


def _response_text(response) -> str:
    content = response.content if hasattr(response, "content") else response
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


# ==============================================================================
# EXTRACCIÓN DE TEXTO
# ==============================================================================

def ocr_with_gemini(llm, data: bytes, mime_type: str) -> str:
    """Envía un archivo (imagen o PDF de una página) a Gemini y regresa la transcripción."""
    block_type = "image" if mime_type.startswith("image/") else "file"
    message = HumanMessage(content=[
        {"type": "text", "text": OCR_PROMPT},
        {"type": block_type, "base64": base64.b64encode(data).decode("ascii"), "mime_type": mime_type},
    ])
    text = _response_text(llm.invoke([message])).strip()
    return "" if text == "[SIN TEXTO]" else text


def _single_page_pdf(reader: PdfReader, page_idx: int) -> bytes:
    writer = PdfWriter()
    writer.add_page(reader.pages[page_idx])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def extract_pdf_pages(llm, path: Path) -> list[dict]:
    """Extrae el texto de cada página; usa OCR con Gemini solo cuando el texto nativo es insuficiente."""
    reader = PdfReader(path)

    def process(page_idx: int) -> dict:
        native = (reader.pages[page_idx].extract_text() or "").strip()
        if not OCR_FORCE and len(native) >= OCR_MIN_CHARS:
            return {"page": page_idx + 1, "text": native, "method": "texto_nativo"}
        ocr_text = ocr_with_gemini(llm, _single_page_pdf(reader, page_idx), "application/pdf")
        # Si el OCR no devuelve nada, se conserva el poco texto nativo que hubiera.
        return {"page": page_idx + 1, "text": ocr_text or native, "method": "ocr_gemini"}

    with ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS) as executor:
        return list(executor.map(process, range(len(reader.pages))))


def extract_file_pages(llm, path: Path) -> list[dict]:
    """Devuelve [{page, text, method}] según el tipo de archivo."""
    ext = path.suffix.lower()
    if ext in PDF_EXTENSIONS:
        return extract_pdf_pages(llm, path)
    if ext in IMAGE_EXTENSIONS:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        return [{"page": 1, "text": ocr_with_gemini(llm, path.read_bytes(), mime_type), "method": "ocr_gemini"}]
    if ext in TEXT_EXTENSIONS:
        return [{"page": 1, "text": path.read_text(encoding="utf-8", errors="replace").strip(), "method": "texto_plano"}]
    raise ValueError(f"Formato no soportado '{ext}'. Formatos válidos: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")


# ==============================================================================
# EJECUCIÓN DEL SPOKE
# ==============================================================================

def run_ocr_agent(file_paths: list[str]) -> dict:
    """Extrae el texto de los archivos cargados. Regresa la evidencia y los documentos extraídos."""
    # Fuera del try: un error de configuración es terminal y no debe convertirse en evidencia.
    llm = get_ocr_llm()

    documents: dict[str, dict] = {}
    files_report: list[dict] = []
    errors: list[str] = []

    for raw_path in file_paths:
        path = Path(raw_path).expanduser()
        try:
            if not path.is_file():
                raise FileNotFoundError(f"No existe el archivo '{path}'")
            pages = extract_file_pages(llm, path)
            pages_with_text = [p for p in pages if p["text"]]
            files_report.append({
                "source": path.name,
                "pages": len(pages),
                "ocr_pages": sum(1 for p in pages if p["method"] == "ocr_gemini"),
                "empty_pages": len(pages) - len(pages_with_text),
                "chars": sum(len(p["text"]) for p in pages_with_text),
            })
            if pages_with_text:
                documents[path.name] = {"total_pages": len(pages), "pages": pages_with_text}
            else:
                errors.append(f"{path.name}: no se pudo extraer texto")
        except Exception as e:
            errors.append(f"{path.name}: {e}")

    lines = [f"Archivos procesados: {len(files_report)}/{len(file_paths)}."]
    for f in files_report:
        lines.append(
            f"- {f['source']}: {f['pages']} pág. ({f['ocr_pages']} con OCR Gemini, "
            f"{f['empty_pages']} sin texto), {f['chars']:,} caracteres extraídos."
        )
    if errors:
        lines.append("Errores: " + "; ".join(errors))

    return {
        "evidence": {
            "files": files_report,
            "extracted_sources": list(documents),
            "summary": "\n".join(lines),
            "error": "; ".join(errors) if errors else None,
        },
        "documents": documents,
    }


def ocr_agent_node(state: dict) -> dict:
    """Nodo de LangGraph para el agente OCR de documentos cargados."""
    result = run_ocr_agent(state.get("uploaded_files") or [])
    evidence = result["evidence"]

    msg_content = f"### [EVIDENCIA OCR - DOCUMENTOS CARGADOS]\n{evidence['summary']}"

    return {
        "evidences": {"ocr_evidence": evidence},
        # El texto extraído viaja en el estado hacia el RAG Agent (y persiste en el hilo para preguntas de seguimiento).
        "uploaded_documents": result["documents"],
        "uploaded_files": [],  # Se consumen los archivos para que no se reprocesen en el siguiente turno
        "messages": [AIMessage(content=msg_content, name="ocr_agent")],
    }
