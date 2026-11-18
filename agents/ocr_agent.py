"""
agents/ocr_agent.py

Spoke de OCR LOCAL para documentos cargados por el usuario.
Flujo:  archivo(s) -> extracción de texto por página (en la máquina local) -> canal 'uploaded_documents' -> RAG Agent
Los archivos NUNCA salen de la máquina: no se envían a Gemini ni se escriben en Pinecone.
Solo el texto extraído llega después al RAG Agent como contexto.

Estrategia de extracción (híbrida):
- PDF digital: se usa el texto nativo de pypdf si la página tiene al menos OCR_MIN_CHARS caracteres.
- PDF escaneado / páginas con poco texto: la página se renderiza a imagen con pypdfium2 (OCR_DPI)
  y se procesa con Tesseract (OCR_LANG, por defecto 'spa+eng').
- Imágenes (PNG/JPG/WEBP/TIFF): Tesseract directamente.
- TXT / MD: lectura directa.
Con OCR_FORCE=true todas las páginas PDF pasan por Tesseract.

Requisito del sistema: el binario de Tesseract con el idioma español.
  macOS:   brew install tesseract tesseract-lang
  Windows: instalador de UB Mannheim (marcar 'Spanish'); si no está en el PATH, definir TESSERACT_CMD.
  Linux:   apt install tesseract-ocr tesseract-ocr-spa
"""

import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps
from pypdf import PdfReader
from langchain_core.messages import AIMessage

load_dotenv(Path(__file__).parent.parent / ".env")

OCR_LANG = os.environ.get("OCR_LANG", "spa+eng")
OCR_DPI = int(os.environ.get("OCR_DPI", "300"))
OCR_MIN_CHARS = int(os.environ.get("OCR_MIN_CHARS", "80"))
OCR_FORCE = os.environ.get("OCR_FORCE", "false").strip().lower() in ("1", "true", "yes", "si", "sí")
OCR_MAX_WORKERS = max(1, min(4, os.cpu_count() or 1))
TESSERACT_CMD = os.environ.get("TESSERACT_CMD")
if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md"}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS


def check_tesseract() -> None:
    """Verifica que Tesseract y los idiomas de OCR_LANG estén instalados; lanza RuntimeError con instrucciones si no."""
    try:
        available = set(pytesseract.get_languages(config=""))
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(
            "Tesseract no está instalado o no está en el PATH. Instálalo (macOS: 'brew install tesseract "
            "tesseract-lang') o define TESSERACT_CMD en el .env con la ruta al ejecutable."
        )
    missing = [lang for lang in OCR_LANG.split("+") if lang not in available]
    if missing:
        raise RuntimeError(
            f"Faltan idiomas de Tesseract: {', '.join(missing)} (instalados: {', '.join(sorted(available))}). "
            "macOS: 'brew install tesseract-lang'; Linux: 'apt install tesseract-ocr-spa'."
        )


# ==============================================================================
# EXTRACCIÓN DE TEXTO (100% LOCAL)
# ==============================================================================

def ocr_image(image: Image.Image) -> str:
    """Aplica Tesseract a una imagen. Escala de grises mejora la precisión en escaneos."""
    gray = ImageOps.grayscale(image)
    return pytesseract.image_to_string(gray, lang=OCR_LANG).strip()


def extract_pdf_pages(path: Path) -> list[dict]:
    """Extrae el texto de cada página; usa Tesseract solo cuando el texto nativo es insuficiente."""
    reader = PdfReader(path)
    pdf = pdfium.PdfDocument(path)
    try:
        pages: list[dict] = []
        pending: list[tuple[int, str]] = []  # (índice, texto nativo) de las páginas que requieren OCR
        for idx, page in enumerate(reader.pages):
            native = (page.extract_text() or "").strip()
            if not OCR_FORCE and len(native) >= OCR_MIN_CHARS:
                pages.append({"page": idx + 1, "text": native, "method": "texto_nativo"})
            else:
                pages.append(None)
                pending.append((idx, native))

        # pypdfium2 no es thread-safe: se renderiza en este hilo y solo Tesseract corre en paralelo.
        # Se procesa por lotes para no tener todas las imágenes de un PDF grande en memoria a la vez.
        with ThreadPoolExecutor(max_workers=OCR_MAX_WORKERS) as executor:
            for start in range(0, len(pending), OCR_MAX_WORKERS):
                batch = pending[start:start + OCR_MAX_WORKERS]
                images = [pdf[idx].render(scale=OCR_DPI / 72).to_pil() for idx, _ in batch]
                for (idx, native), text in zip(batch, executor.map(ocr_image, images)):
                    # Si el OCR no devuelve nada, se conserva el poco texto nativo que hubiera.
                    pages[idx] = {"page": idx + 1, "text": text or native, "method": "ocr_tesseract"}
        return pages
    finally:
        pdf.close()


def extract_file_pages(path: Path) -> list[dict]:
    """Devuelve [{page, text, method}] según el tipo de archivo."""
    ext = path.suffix.lower()
    if ext in PDF_EXTENSIONS:
        return extract_pdf_pages(path)
    if ext in IMAGE_EXTENSIONS:
        with Image.open(path) as img:
            image = ImageOps.exif_transpose(img)  # respeta la orientación de fotos tomadas con celular
            return [{"page": 1, "text": ocr_image(image), "method": "ocr_tesseract"}]
    if ext in TEXT_EXTENSIONS:
        return [{"page": 1, "text": path.read_text(encoding="utf-8", errors="replace").strip(), "method": "texto_plano"}]
    raise ValueError(f"Formato no soportado '{ext}'. Formatos válidos: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")


# ==============================================================================
# EJECUCIÓN DEL SPOKE
# ==============================================================================

def run_ocr_agent(file_paths: list[str]) -> dict:
    """Extrae el texto de los archivos cargados. Regresa la evidencia y los documentos extraídos."""
    documents: dict[str, dict] = {}
    files_report: list[dict] = []
    errors: list[str] = []

    needs_ocr = any(Path(p).suffix.lower() in PDF_EXTENSIONS | IMAGE_EXTENSIONS for p in file_paths)
    if needs_ocr:
        try:
            check_tesseract()
        except RuntimeError as e:
            return {
                "evidence": {"files": [], "extracted_sources": [], "summary": f"Error en OCR Agent: {e}", "error": str(e)},
                "documents": {},
            }

    for raw_path in file_paths:
        path = Path(raw_path).expanduser()
        try:
            if not path.is_file():
                raise FileNotFoundError(f"No existe el archivo '{path}'")
            pages = extract_file_pages(path)
            pages_with_text = [p for p in pages if p["text"]]
            files_report.append({
                "source": path.name,
                "pages": len(pages),
                "ocr_pages": sum(1 for p in pages if p["method"] == "ocr_tesseract"),
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
            f"- {f['source']}: {f['pages']} pág. ({f['ocr_pages']} con OCR local Tesseract, "
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
    """Nodo de LangGraph para el agente OCR local de documentos cargados."""
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
