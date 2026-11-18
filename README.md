# Plataforma Multi-Agente Hub-and-Spoke - Data Steward Banamex

Sistema inteligente multi-agente diseñado bajo la arquitectura **Hub-and-Spoke (Supervisor Pattern)** con **LangGraph** para la democratización y gobernanza de datos en **Grupo Financiero Banamex**.

Un **Coordinador (Hub)** recibe cada pregunta, decide qué **agente especializado (Spoke)** debe atenderla, le entrega una instrucción enriquecida con el contexto de la conversación y, al recibir la evidencia, sintetiza una respuesta final con la fuente del dato. Cuando el usuario carga documentos, un **procesador OCR local** extrae su texto y se lo entrega al agente RAG.

---

## Arquitectura del Sistema

```
                  ┌────────────────────────────────────────────┐
                  │           Usuario (CLI: main.py)           │
                  └──────┬──────────────────────────────┬──────┘
                pregunta │                              │ + archivos
                         ▼                              ▼
              ┌─────────────────────┐       ┌───────────────────────┐
              │  COORDINADOR (HUB)  │       │     PROCESADOR OCR    │
              │ ruteo + instrucción │       │    Tesseract local    │
              │     enriquecida     │       │   nodo determinista   │
              └──────────┬──────────┘       │     (no es agente)    │
                         │                  └───────────┬───────────┘
        ┌────────────────┼──────────────────┐           │ texto extraído
        ▼                ▼                  ▼           ▼
┌───────────────┐ ┌─────────────┐   ┌─────────────────────────┐
│   SQL AGENT   │ │  WEB SEARCH │   │        RAG AGENT        │
│    (Spoke)    │ │   (Spoke)   │   │         (Spoke)         │
│               │ │             │   │                         │
│     Gemini    │ │    Gemini   │   │    Gemini + Pinecone    │
│   PostgreSQL  │ │    Tavily   │   │    bge-large (Ollama)   │
│   dim & cfg   │ │   noticias  │   │     corpus oficial +    │
└───────┬───────┘ └──────┬──────┘   │      docs cargados      │
        │                │          └────────────┬────────────┘
        └────────────────┼───────────────────────┘ (evidencias)
                         ▼
              ┌─────────────────────┐
              │  COORDINADOR (HUB)  │
              │ síntesis y auditoría│
              └──────────┬──────────┘
                         ▼
               Respuesta fundamentada
```

### Componentes

| Componente | Tipo | Qué hace | Tecnología |
|---|---|---|---|
| **Coordinador** (`agents/coordinator.py`) | Hub (agente) | Decide si responde con la memoria de la conversación o delega a un spoke; reescribe la pregunta como instrucción técnica (`spoke_instruction`) y sintetiza la respuesta final citando la fuente. | Google Gemini |
| **SQL Agent** (`agents/sql_agent.py`) | Spoke (agente) | Text-to-SQL sobre el modelo dimensional. El esquema y las reglas de negocio están descritos a mano en el prompt (capa semántica). | Gemini + PostgreSQL |
| **RAG Agent** (`agents/rag_agent.py`) | Spoke (agente) | Recupera 4 fragmentos del corpus oficial en Pinecone y, si hay documentos cargados en la conversación, también usa su texto. Responde citando documento y página. | Gemini + Pinecone + bge-large |
| **Web Search Agent** (`agents/web_search_agent.py`) | Spoke (agente) | Búsqueda de información pública y reciente. | Gemini + Tavily |
| **Procesador OCR** (`processing/ocr_processor.py`) | Nodo determinista (no es agente) | Extrae el texto de los archivos cargados, página por página, y lo deja en el estado para el RAG Agent. No usa LLM ni Pinecone. | pypdf + pypdfium2 + Tesseract |

### Flujos del grafo (`workflow.py`)

- **Pregunta normal:** `START → Coordinador → (SQL | RAG | Web) → Coordinador (síntesis) → END`. El Coordinador también puede responder directo (`FINISH`) desde la memoria.
- **Pregunta con archivos:** `START → Procesador OCR → RAG Agent → Coordinador (síntesis) → END`. Si no se pudo extraer texto, el procesador OCR pasa directo al Coordinador, que reporta el error.
- **Un spoke por turno:** aún no hay ejecución en paralelo de varios spokes.

### Procesador OCR (documentos cargados)

- **PDF con texto digital:** se lee directamente con pypdf (sin OCR).
- **PDF escaneado o páginas con poco texto** (`OCR_MIN_CHARS`): la página se renderiza a imagen con pypdfium2 (`OCR_DPI`) y se lee con **Tesseract** en español/inglés (`OCR_LANG=spa+eng`).
- **Imágenes** (PNG, JPG, WEBP, TIFF, BMP): Tesseract directo. **TXT/MD:** lectura directa.
- **Privacidad:** todo corre en la máquina local; los archivos no se envían a Gemini ni a Pinecone. Solo el texto extraído llega al RAG Agent como contexto.
- **Memoria:** el texto se guarda en el canal `uploaded_documents` del estado, así que se pueden hacer preguntas de seguimiento sobre el documento en la misma sesión. Se pierde al cerrar el programa.

---

## Estructura del Repositorio

```
Banamex/
├── agents/                       # Agentes con LLM (Hub y Spokes)
│   ├── coordinator.py            # Hub: ruteo, instrucción enriquecida y síntesis final
│   ├── llm.py                    # Fábrica única del cliente Gemini (ConfigError si falta la API key)
│   ├── sql_agent.py              # Spoke: Text-to-SQL sobre PostgreSQL
│   ├── rag_agent.py              # Spoke: RAG sobre Pinecone + documentos cargados
│   └── web_search_agent.py       # Spoke: búsqueda web con Tavily
├── processing/                   # Nodos deterministas del grafo (sin LLM)
│   └── ocr_processor.py          # OCR local de documentos cargados (Tesseract)
├── data/
│   ├── terminos_banamex.txt      # Términos y condiciones del portal Banamex
│   ├── ingest.py                 # Indexa solo terminos_banamex.txt en Pinecone (aditivo)
│   ├── ingest_all.py             # Indexa el corpus completo de PDFs + términos (BORRA el índice antes)
│   ├── reto_banamex_completo.zip # Corpus de PDFs (descomprimir en data/reto_banamex_completo/)
│   └── reto_banamex_completo.sql # Dump de PostgreSQL (esquemas dim y cfg)
├── state.py                      # AgentState tipado y reducers (evidencias, documentos cargados)
├── workflow.py                   # StateGraph compilado con checkpointing (MemorySaver)
├── main.py                       # CLI: demo, modo interactivo y carga de archivos
├── logging_config.py             # Paneles Rich para la trazabilidad de cada paso
├── .env.example                  # Plantilla de variables de entorno
└── pyproject.toml / uv.lock      # Dependencias gestionadas con uv
```

---

## Requisitos Previos

1. **Python >= 3.12** y [uv](https://docs.astral.sh/uv/).
2. **API key de Google Gemini** (`GOOGLE_API_KEY`): es el único proveedor de LLM del Coordinador y los tres spokes. Sin ella el programa se detiene con un error.
3. **Ollama** local con el modelo de embeddings (solo embeddings, no LLM):
   ```bash
   ollama pull bge-large
   ```
4. **PostgreSQL** con la base `reto_banamex`, cargada desde `data/reto_banamex_completo.sql`.
5. Cuentas y API keys de **Pinecone** y **Tavily**.
6. **Tesseract** con el idioma español, para el procesador OCR:
   - macOS: `brew install tesseract tesseract-lang`
   - Windows: instalador de [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) marcando *Spanish*; si no queda en el PATH, define `TESSERACT_CMD` en el `.env`.
   - Linux: `sudo apt install tesseract-ocr tesseract-ocr-spa`

---

## Instalación y Puesta en Marcha

### 1. Clonar el repositorio
```bash
git clone <URL_DEL_REPOSITORIO>
cd Banamex
```

### 2. Sincronizar el entorno virtual con `uv`
```bash
uv sync
```

### 3. Configurar variables de entorno
```bash
cp .env.example .env
```
Configura al menos:
- `GOOGLE_API_KEY`, `PINECONE_API_KEY`, `TAVILY_API_KEY`
- `INDEX_NAME=banamex-governance`
- `PGUSER`, `PGPASSWORD`, `PGDATABASE=reto_banamex`
- Opcional: `COORDINATOR_MODEL` / `SQL_MODEL` (modelos de Gemini) y las variables `OCR_*`.

### 4. Indexar documentos en Pinecone (una sola vez)
```bash
# Corpus completo (descomprime antes data/reto_banamex_completo.zip). BORRA el índice primero.
uv run python data/ingest_all.py

# O solo los términos y condiciones (aditivo):
uv run python data/ingest.py
```

### 5. Ejecutar
```bash
# Demo: una pregunta para cada spoke (SQL, RAG y Web)
uv run python main.py --demo

# Consola interactiva con memoria de conversación
uv run python main.py --interactive
#   dentro de la consola:  /subir "ruta/al documento.pdf"  y luego la pregunta (Enter = resumen)

# Cargar archivos y preguntar sobre ellos en un solo comando
uv run python main.py --ocr estado_cuenta.pdf foto_contrato.jpg --pregunta "¿Qué comisiones aparecen?"
```

---

## Características de Gobernanza y Data Stewardship

- **Cero Datos Huérfanos:** las consultas del `sql_agent` se ejecutan sobre un modelo dimensional con llaves foráneas (`dim_cliente`, `dim_producto`), con reglas de negocio explícitas (p. ej. filtrar los saldos mensuales a un solo corte).
- **Verificabilidad y Trazabilidad:** cada respuesta final cita la fuente del dato: PostgreSQL interno, corpus oficial en Pinecone, documento cargado por el usuario o búsqueda web con Tavily.
- **Privacidad de documentos cargados:** el OCR corre localmente; los archivos del usuario no se suben a servicios externos ni se mezclan con el índice oficial en Pinecone.
- **Memoria de Sesión (LangGraph Checkpointing):** `MemorySaver` conserva el hilo de la conversación (incluyendo los documentos cargados) para preguntas de seguimiento.
- **Errores como evidencia:** las fallas en tiempo de ejecución (SQL inválido, servicio caído, OCR sin texto) se devuelven como evidencia y el Coordinador las comunica, en vez de detener el sistema. Solo la falta de `GOOGLE_API_KEY` es un error terminal.
