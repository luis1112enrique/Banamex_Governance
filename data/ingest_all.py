"""
data/ingest_all.py

Script de Ingesta Oficial y Vectorización del Corpus Bancario Curado de Banamex.
Pipeline estandarizado para indexación en Pinecone (banamex-governance):
- 42 Documentos Curados de Negocio: Folletos comerciales de tarjetas, Anexos oficiales de comisiones,
  Créditos clave (Personal, Nómina, Hipotecario), Fichas y Comisiones de Cuentas/Débito/Ahorro,
  Términos web oficiales y el Glosario Oficial de Métricas de Riesgo y Cosechas (Markdown).
- EXCLUSIÓN AUTOMÁTICA: Excluye marcos normativos macro (leyes y circulares de 100+ págs),
  contratos maestros legales redundantes, formatos capturables en blanco y archivos CSV/ZIP.
- Embeddings: Ollama 'bge-large' (1024 dimensiones) con paralelismo concurrente (ThreadPoolExecutor).
- Base Vectorial: Pinecone Serverless (AWS us-east-1, métrica cosine, dimensión 1024).
"""

import sys
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8")
import os
import time
import argparse
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

from pypdf import PdfReader
from pinecone import Pinecone, ServerlessSpec
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_ollama import OllamaEmbeddings

# Cargar variables de entorno
load_dotenv(Path(__file__).parent.parent / ".env")

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
INDEX_NAME = os.environ.get("INDEX_NAME", "banamex-governance")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-large")

DATA_DIR = Path(__file__).parent
CORPUS_DIR = DATA_DIR / "reto_banamex_completo"
TERMINOS_TXT = DATA_DIR / "terminos_banamex.txt"
GLOSARIO_MD = DATA_DIR / "glosario_metricas_riesgo_tarjetas.md"
GLOSARIO_MD_ALT = CORPUS_DIR / "glosario_metricas_riesgo_tarjetas.md"

# Reglas estrictas de exclusión de ruido semántico
EXCLUDE_DIRS = {
    "00_Contratos_Maestros",
    "00_INDICE",
    "03_Terminos_Beneficios",
    "05_Marco_Normativo",
    "01_Contratos",      # Contratos legales extensos dentro de Cuentas_Nomina_Ahorro
    "Cap_Sem_Banamex",   # Archivos CSV de ingeniería (se consumen en SQL, no en RAG)
}

EXCLUDE_FILES = {
    "credito_hipotecario_formato_solicitud.pdf",  # Formato en blanco capturable
    "credito_hipotecario_medios_pago.pdf",        # Lista prescindible de ventanillas
    "credito_nomina_medios_pago.pdf",             # Redundante con canales bancarios
    "README.md",
    "SHA256SUMS.txt",
}

EXCLUDE_EXTS = {".csv", ".zip", ".py", ".sql", ".json", ".png", ".jpg", ".jpeg", ".webp"}


def ensure_clean_pinecone_index(pc: Pinecone, index_name: str, dimension: int = 1024, recreate: bool = False):
    """
    Garantiza un índice limpio y listo en Pinecone:
    - Si 'recreate=True' o el índice no existe, lo crea con la configuración óptima.
    - Si el índice ya existe, purga todos los vectores previos para evitar mezclas con versiones antiguas.
    """
    existing_indexes = [i.name for i in pc.list_indexes()]

    if recreate and index_name in existing_indexes:
        print(f"🗑️ Eliminando índice existente '{index_name}' por bandera --recreate...", flush=True)
        pc.delete_index(index_name)
        while index_name in [i.name for i in pc.list_indexes()]:
            time.sleep(2)
        existing_indexes.remove(index_name)

    if index_name not in existing_indexes:
        print(f"🚀 Creando índice nuevo '{index_name}' en Pinecone (dim={dimension}, metric='cosine', aws/us-east-1)...", flush=True)
        pc.create_index(
            name=index_name,
            dimension=dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        while not pc.describe_index(index_name).status["ready"]:
            time.sleep(2)
        print("✅ Índice creado y listo en Pinecone.", flush=True)
    else:
        print(f"🔄 Índice '{index_name}' activo en Pinecone. Purgando vectores anteriores...", flush=True)
        idx = pc.Index(index_name)
        try:
            idx.delete(delete_all=True)
            print("✅ Vectores antiguos purgados exitosamente. Partiendo de índice en blanco.", flush=True)
        except Exception as e:
            print(f"⚠️ Aviso al purgar vectores: {e}", flush=True)


def generate_stable_id(doc: Document) -> str:
    """Genera un hash determinista SHA-256 para identificar unívocamente cada chunk."""
    identity = f"{doc.metadata.get('source')}|p{doc.metadata.get('page', 1)}|{doc.page_content[:140]}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def infer_doc_metadata(file_path: Path, page_idx: int, total_pages: int) -> dict:
    """Infiere metadatos estructurados para auditoría y filtrado RAG."""
    fname = file_path.name.lower()
    parent = file_path.parent.name.lower()

    if fname.startswith("folleto_"):
        doc_type = "folleto_comercial"
        priority = 2
    elif fname.startswith("anexo_") or fname.startswith("comisiones_"):
        doc_type = "anexo_comisiones"
        priority = 3
    elif fname.startswith("ficha_"):
        doc_type = "ficha_contexto_web"
        priority = 2
    elif "credito" in fname:
        doc_type = "credito_banamex"
        priority = 4
    elif "ahorro" in fname or "gat" in fname or "tasas" in fname:
        doc_type = "ahorro_inversiones"
        priority = 3
    elif fname.endswith(".md"):
        doc_type = "glosario_metricas_riesgo"
        priority = 1
    else:
        doc_type = "documento_bancario"
        priority = 3

    # Extraer nombre amigable de producto o familia
    product = fname.replace("folleto_", "").replace("anexo_", "").replace("credito_", "").replace(".pdf", "").replace(".md", "").replace(".txt", "")

    return {
        "source": file_path.name,
        "tipo_documento": doc_type,
        "producto_o_familia": product,
        "prioridad_rag": priority,
        "institucion": "Grupo Financiero Banamex",
        "page": page_idx,
        "total_pages": total_pages,
    }


def load_curated_corpus() -> list[Document]:
    """
    Escanea de forma autónoma el corpus curado de Banamex:
    Aplica las reglas de exclusión y retorna la lista de Documents de LangChain.
    """
    raw_docs: list[Document] = []
    processed_files = []
    skipped_files = []

    # 1. Glosario de Métricas de Riesgo y Cosechas (Markdown)
    glossary_path = GLOSARIO_MD if GLOSARIO_MD.exists() else GLOSARIO_MD_ALT
    if glossary_path.exists():
        text = glossary_path.read_text(encoding="utf-8").strip()
        if text:
            raw_docs.append(Document(
                page_content=text,
                metadata={
                    "source": glossary_path.name,
                    "tipo_documento": "glosario_metricas_riesgo",
                    "producto_o_familia": "tarjetas_credito_riesgo_cosechas",
                    "prioridad_rag": 1,
                    "institucion": "Grupo Financiero Banamex",
                    "page": 1,
                    "total_pages": 1,
                }
            ))
            processed_files.append((glossary_path.name, 1))

    # 2. Términos del Portal Web
    if TERMINOS_TXT.exists():
        text = TERMINOS_TXT.read_text(encoding="utf-8").strip()
        if text:
            raw_docs.append(Document(
                page_content=text,
                metadata={
                    "source": TERMINOS_TXT.name,
                    "tipo_documento": "terminos_portal_web",
                    "producto_o_familia": "portal_banamex_general",
                    "prioridad_rag": 4,
                    "institucion": "Grupo Financiero Banamex",
                    "page": 1,
                    "total_pages": 1,
                }
            ))
            processed_files.append((TERMINOS_TXT.name, 1))

    # 3. Folletos Comerciales Oficiales Digitalizados (Markdown con 100% de texto y beneficios)
    folletos_md_dir = DATA_DIR / "folletos_md"
    loaded_folletos = set()
    if folletos_md_dir.exists():
        for f_path in sorted(folletos_md_dir.glob("*.md")):
            txt = f_path.read_text(encoding="utf-8").strip()
            if txt:
                meta = infer_doc_metadata(f_path, 1, 1)
                raw_docs.append(Document(page_content=txt, metadata=meta))
                processed_files.append((f_path.name, 1))
                loaded_folletos.add(f_path.stem)

    # 4. Documentos Oficiales en PDF dentro del corpus
    if CORPUS_DIR.exists():
        for root, dirs, files in os.walk(CORPUS_DIR):
            parts = Path(root).parts
            # Exclusión de carpetas no deseadas
            if any(ex in parts for ex in EXCLUDE_DIRS):
                continue

            for f in sorted(files):
                f_path = Path(root) / f
                ext = f_path.suffix.lower()

                # Exclusión de archivos y extensiones
                if f in EXCLUDE_FILES or ext in EXCLUDE_EXTS or f == glossary_path.name:
                    skipped_files.append(f)
                    continue

                # Si es un folleto en Markdown dentro de la carpeta y ya se cargó, no duplicar
                if ext == ".md":
                    if f_path.stem in loaded_folletos:
                        continue
                    try:
                        txt = f_path.read_text(encoding="utf-8").strip()
                        if txt:
                            meta = infer_doc_metadata(f_path, 1, 1)
                            raw_docs.append(Document(page_content=txt, metadata=meta))
                            processed_files.append((f, 1))
                            loaded_folletos.add(f_path.stem)
                    except Exception as e:
                        print(f"⚠️ Error al procesar {f}: {e}", flush=True)
                    continue

                if ext == ".pdf":
                    # Si ya cargamos la versión Markdown de este folleto, evitamos procesar el PDF de imagen
                    if f_path.stem in loaded_folletos:
                        continue

                    try:
                        reader = PdfReader(f_path)
                        n_pages = len(reader.pages)
                        pages_with_text = 0

                        for p_idx, page in enumerate(reader.pages, 1):
                            txt = (page.extract_text() or "").strip()
                            if not txt:
                                continue
                            pages_with_text += 1
                            meta = infer_doc_metadata(f_path, p_idx, n_pages)
                            raw_docs.append(Document(page_content=txt, metadata=meta))

                        if pages_with_text > 0:
                            processed_files.append((f, pages_with_text))
                    except Exception as e:
                        print(f"⚠️ Error al procesar {f}: {e}", flush=True)

    print(f"\n📂 Archivos curados aceptados para ingesta: {len(processed_files)}")
    for name, p_count in processed_files:
        print(f"   ✔️ {name:<55} ({p_count} págs)")
    print(f"🚫 Archivos ignorados / excluidos: {len(skipped_files)} (CSVs, normativas y contratos extensos)")

    return raw_docs


def main():
    parser = argparse.ArgumentParser(description="Ingesta y Vectorización en Pinecone del Corpus Curado Banamex")
    parser.add_argument("--recreate", action="store_true", help="Elimina y vuelve a crear el índice en Pinecone desde cero.")
    args = parser.parse_args()

    if not PINECONE_API_KEY:
        raise ValueError("❌ PINECONE_API_KEY no encontrada en .env")

    print("\n" + "=" * 80, flush=True)
    print(" INGESTA DE CORPUS BANCARIO CURADO BANAMEX EN PINECONE (ALTA PRECISIÓN)")
    print("=" * 80, flush=True)

    pc = Pinecone(api_key=PINECONE_API_KEY)
    ensure_clean_pinecone_index(pc, INDEX_NAME, dimension=1024, recreate=args.recreate)
    index = pc.Index(INDEX_NAME)

    # 1. Cargar documentos
    print("\n[1/4] Extrayendo páginas de PDFs de productos, comisiones, glosario y términos...", flush=True)
    all_raw_docs = load_curated_corpus()
    print(f"\n  * Total de páginas con texto útil procesadas: {len(all_raw_docs)}", flush=True)

    if not all_raw_docs:
        print("❌ No se encontraron documentos válidos para indexar.", flush=True)
        return

    # 2. Particionar en Chunks semánticos
    print("\n[2/4] Generando chunks semánticos (chunk_size=1000, overlap=150)...", flush=True)
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=150,
        separators=["\n\n", "\n", " ", ""]
    )
    chunks = splitter.split_documents(all_raw_docs)
    total_chunks = len(chunks)
    print(f"  * Total de chunks semánticos listos para indexar: {total_chunks}", flush=True)

    # 3. Inicializar embeddings acelerados
    print(f"\n[3/4] Conectando a Ollama ('{EMBEDDING_MODEL}') con concurrencia...", flush=True)
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)

    # 4. Upsert en lotes concurrentes a Pinecone
    print(f"\n[4/4] Indexando {total_chunks} chunks en Pinecone ('{INDEX_NAME}')...", flush=True)
    batch_size = 50
    t0 = time.time()

    for i in range(0, total_chunks, batch_size):
        end = min(i + batch_size, total_chunks)
        batch_chunks = chunks[i:end]

        texts = [c.page_content for c in batch_chunks]
        ids = [generate_stable_id(c) for c in batch_chunks]

        # Paralelismo de 4 hilos para acelerar embeddings locales
        with ThreadPoolExecutor(max_workers=4) as executor:
            vectors = list(executor.map(embeddings.embed_query, texts))

        pinecone_records = []
        for vec_id, vec, c in zip(ids, vectors, batch_chunks):
            meta = dict(c.metadata)
            meta["text"] = c.page_content
            pinecone_records.append({
                "id": vec_id,
                "values": vec,
                "metadata": meta
            })

        index.upsert(vectors=pinecone_records)

        elapsed = time.time() - t0
        pct = (end / total_chunks) * 100
        speed = end / elapsed if elapsed > 0 else 0
        eta = (total_chunks - end) / speed if speed > 0 else 0
        print(f"  -> [{pct:5.1f}%] {end}/{total_chunks} chunks ({speed:.1f} ch/s, transcurrido: {elapsed:.0f}s, ETA: {eta:.0f}s)...", flush=True)

    print(f"\n🎉 ¡Éxito total! Se indexaron {total_chunks} chunks en Pinecone ('{INDEX_NAME}').", flush=True)
    print(f"⏱️ Tiempo total: {time.time()-t0:.1f} segundos.\n", flush=True)


if __name__ == "__main__":
    main()
