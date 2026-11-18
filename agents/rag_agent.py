"""
agents/rag_agent.py

Spoke especializado en gobernanza documental, comisiones y normativa bancaria (RAG).
Consulta el índice semántico 'banamex-governance' en Pinecone generado con bge-large
sobre el corpus oficial de Banamex (59 PDFs de contratos, comisiones, folletos, circulares Banxico y términos).
Genera respuestas fundamentadas citando fuentes oficiales con Google Gemini (gemini-3.5-flash).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

from langchain_core.messages import AIMessage
from langchain_core.prompts import PromptTemplate
from langchain_ollama import OllamaEmbeddings
from langchain_pinecone import PineconeVectorStore

from agents.llm import get_gemini_llm

load_dotenv(Path(__file__).parent.parent / ".env")

PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
INDEX_NAME = os.environ.get("INDEX_NAME", "banamex-governance")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-large")
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", "gemini-3.5-flash")
MAX_UPLOADED_CHARS = 300_000  # tope de texto de documentos cargados que se envía al LLM


def get_vector_store() -> PineconeVectorStore:
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    return PineconeVectorStore(
        index_name=INDEX_NAME,
        embedding=embeddings,
    )


def get_rag_llm():
    """Retorna Gemini. Lanza ConfigError (error terminal) si falta GOOGLE_API_KEY."""
    return get_gemini_llm(COORDINATOR_MODEL)


def format_uploaded_documents(uploaded_documents: dict | None) -> list[str]:
    """Convierte el texto extraído por el procesador OCR (canal 'uploaded_documents') en bloques de contexto.
    Recorta al llegar a MAX_UPLOADED_CHARS para no exceder el contexto del modelo."""
    blocks, used = [], 0
    for source, doc in (uploaded_documents or {}).items():
        for p in doc.get("pages", []):
            text = p["text"].strip()
            if used + len(text) > MAX_UPLOADED_CHARS:
                blocks.append(f"[Documento cargado | Fuente: {source}] (texto restante omitido por longitud)")
                return blocks
            used += len(text)
            blocks.append(
                f"[Documento cargado | Fuente: {source} (Pág. {p['page']} de {doc.get('total_pages', '?')}, "
                f"Tipo: documento_cargado, Extracción: {p.get('method', 'ocr')})]\n{text}"
            )
    return blocks


def run_rag_agent(question: str, uploaded_documents: dict | None = None) -> dict:
    """
    Recupera contexto de Pinecone y genera una respuesta fundamentada con Gemini 3.5 Flash.
    Si hay 'uploaded_documents' (texto extraído por el procesador OCR), se agregan al contexto
    directamente; no se buscan ni se guardan en Pinecone.
    """
    # Fuera del try: un error de configuración es terminal y no debe convertirse en evidencia.
    llm = get_rag_llm()

    try:
        uploaded_blocks = format_uploaded_documents(uploaded_documents)

        try:
            docs = get_vector_store().as_retriever(search_kwargs={"k": 4}).invoke(question)
        except Exception as e:
            # Con documentos cargados aún se puede responder aunque Pinecone/Ollama no estén disponibles.
            if not uploaded_blocks:
                raise
            docs = []
            uploaded_blocks.append(f"[Aviso: no se pudo consultar el corpus oficial en Pinecone: {e}]")

        if not docs and not uploaded_blocks:
            return {
                "retrieved_docs": [],
                "summary": "No se encontró evidencia relevante en la documentación oficial de Banamex.",
                "error": None
            }

        context_blocks = list(uploaded_blocks)
        for i, d in enumerate(docs, 1):
            source = d.metadata.get("source", "documento_banamex")
            page = d.metadata.get("page", 1)
            tipo = d.metadata.get("tipo_documento", "documento")
            prod = d.metadata.get("producto_o_familia", "")
            context_blocks.append(
                f"[Fragmento {i} | Fuente: {source} (Pág. {page}, Tipo: {tipo}, Producto: {prod})]\n{d.page_content.strip()}"
            )

        context_text = "\n\n---\n\n".join(context_blocks)

        prompt = PromptTemplate.from_template(
            """Eres el Oficial de Cumplimiento, Comisiones y Gobernanza Documental de Banamex.
Responde a la siguiente consulta del usuario basándote ESTRICTAMENTE en la documentación oficial de Banamex recuperada (contratos, anexos de comisiones, folletos o normativas).
Si la información no se encuentra en el texto proporcionado, indícalo claramente sin inventar.

### TEXTO OFICIAL RECUPERADO DE BANAMEX:
{context}

### PREGUNTA O INSTRUCCIÓN:
{question}

### REGLAS DE RESPUESTA:
1. Precisión y Citas: Cita el nombre del documento oficial (ej. anexo_costco.pdf, contrato_cuenta_base.pdf), numerales, comisiones exactas en pesos o porcentajes y páginas.
2. Si se trata de comisiones o costos, sé muy específico sobre montos sin IVA, anualidades o penalizaciones.
3. Mantén un tono formal, bancario, transparente y verificado.
4. Los bloques marcados como 'Documento cargado' provienen de un archivo subido por el usuario (procesado con OCR), no del corpus oficial: indícalo al citarlo. Si la pregunta es sobre ese archivo, prioriza su contenido y usa los fragmentos oficiales solo para contrastar o complementar.
"""
        )

        chain = prompt | llm
        response = chain.invoke({"context": context_text, "question": question})
        content = response.content if hasattr(response, "content") else str(response)
        if isinstance(content, list):
            rag_answer = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        else:
            rag_answer = str(content)

        return {
            "retrieved_docs": context_blocks,
            "summary": rag_answer,
            "error": None
        }

    except Exception as e:
        return {
            "retrieved_docs": [],
            "summary": f"Error en RAG Agent: {str(e)}",
            "error": str(e)
        }


def rag_agent_node(state: dict) -> dict:
    """Nodo de LangGraph para el agente RAG normativo."""
    instruction = state.get("spoke_instruction") or state.get("question", "")
    # Texto de los documentos cargados en este hilo (extraído por el procesador OCR, no indexado en Pinecone).
    result = run_rag_agent(instruction, uploaded_documents=state.get("uploaded_documents"))

    msg_content = f"### [EVIDENCIA RAG - DOCUMENTACIÓN OFICIAL BANAMEX]\n{result['summary']}"

    return {
        "evidences": {"rag_evidence": result},
        "messages": [AIMessage(content=msg_content, name="rag_agent")]
    }
