"""
main.py

Punto de entrada de prueba para la Plataforma Multi-Agente Hub-and-Spoke de Banamex.
Incluye selección de perfil RBAC (Usuario Universal vs. Cliente Final Banamex)
y trazabilidad completa del Hub y los Spokes.
"""

import os
import sys
import shlex
import shutil
if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from typing import Optional, Tuple, List
from dotenv import load_dotenv

# Cargar variables de entorno ANTES de importar LangGraph/LangChain para activar LangSmith desde el inicio
load_dotenv(Path(__file__).parent / ".env")

from langchain_core.messages import HumanMessage
from workflow import multi_agent_app
from agents.sql_agent import validate_cliente_id
from logging_config import (
    log_header,
    log_coordinator_decision,
    log_sql_execution,
    log_rag_retrieval,
    log_chart_generation,
    log_doc_analysis,
    log_final_response,
)

DEFAULT_UPLOAD_QUESTION = "Inspecciona los documentos cargados, clasifícalos y valida si cumplen con los requisitos oficiales de Banamex."


def cleanup_session_files(thread_id: str):
    """Elimina de forma segura la carpeta temporal de archivos de la sesión (inputs/{thread_id})."""
    session_dir = Path(__file__).parent / "inputs" / thread_id
    if session_dir.exists():
        shutil.rmtree(session_dir, ignore_errors=True)


def stage_session_files(files: List[str], thread_id: str) -> List[str]:
    """Copia los archivos a la carpeta aislada de la sesión (inputs/{thread_id}) para evitar mezclas."""
    session_dir = Path(__file__).parent / "inputs" / thread_id
    session_dir.mkdir(parents=True, exist_ok=True)
    staged_paths = []
    for raw in files:
        src = Path(raw).expanduser().resolve()
        if src.is_file():
            dst = session_dir / src.name
            if src != dst:
                shutil.copy2(src, dst)
            staged_paths.append(str(dst))
    return staged_paths


def ask_data_steward(
    question: str,
    thread_id: str = "demo_thread",
    user_role: str = "universal",
    cliente_id: Optional[int] = None,
    auto_open_chart: bool = False,
    files: Optional[List[str]] = None,
) -> dict:
    """Ejecuta el grafo Hub-and-Spoke Multi-Hop trazando cada paso en consola y LangSmith."""
    files_clean = stage_session_files(files or [], thread_id) if files else []
    if files_clean:
        file_names = ", ".join(Path(f).name for f in files_clean)
        question = f"{question or DEFAULT_UPLOAD_QUESTION} (Documento(s) cargado(s): {file_names})"

    role_badge = "UNIVERSAL" if user_role == "universal" else f"CLIENTE num_cte={cliente_id}"
    log_header(f"[{role_badge}] CONSULTA RECIBIDA: {question}")

    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": f"Banamex_Data_Steward [{role_badge}]",
        "tags": [
            f"role:{user_role}",
            "hub-and-spoke",
            "banamex-data-steward",
        ],
        "metadata": {
            "user_role": user_role,
            "cliente_id": cliente_id,
            "thread_id": thread_id,
        },
    }
    initial_input = {
        "messages": [HumanMessage(content=question)],
        "question": question,
        "user_role": user_role,
        "cliente_id": cliente_id,
        "plan": None,
        "next_agent": None,
        "spoke_instruction": None,
        "evidences": {},
        "uploaded_files": files_clean,
        "chart_path": None,
        "step_count": 0,
        "final_answer": None,
    }

    final_answer = None
    chart_path = None
    for event in multi_agent_app.stream(initial_input, config=config, stream_mode="updates"):
        for node_name, update in event.items():
            if node_name == "coordinator":
                if update.get("plan") and update.get("next_agent"):
                    log_coordinator_decision(update["plan"], update["next_agent"])
                if update.get("final_answer"):
                    final_answer = update["final_answer"]
                if update.get("chart_path"):
                    chart_path = update["chart_path"]
            elif node_name == "sql_agent":
                ev = update.get("evidences", {}).get("sql_evidence", {})
                log_sql_execution(ev.get("sql_query", "N/A"), ev.get("summary", "N/A"))
            elif node_name == "rag_agent":
                ev = update.get("evidences", {}).get("rag_evidence", {})
                log_rag_retrieval(question, ev.get("summary", "N/A"))
            elif node_name == "doc_agent":
                ev = update.get("evidences", {}).get("doc_evidence", {})
                log_doc_analysis([Path(f).name for f in (files_clean or [])], ev.get("summary", "N/A"))
            elif node_name == "chart_tool":
                ev = update.get("evidences", {}).get("chart_evidence", {})
                if update.get("chart_path"):
                    chart_path = update["chart_path"]
                log_chart_generation(
                    ev.get("title", "Gráfica Banamex"),
                    ev.get("chart_type", "N/A"),
                    ev.get("chart_path") or "N/A",
                    ev.get("summary", "N/A"),
                )

    if final_answer:
        log_final_response(final_answer, chart_path=chart_path)

    if auto_open_chart and chart_path and Path(chart_path).exists():
        try:
            os.startfile(chart_path)
        except Exception:
            pass

    return {"final_answer": final_answer, "chart_path": chart_path}


def authenticate_user() -> Tuple[str, Optional[int]]:
    """
    Flujo de selección de perfil en consola (sin contraseña por el momento):
      - Opción 1: Usuario Universal (acceso analítico completo a SQL y RAG).
      - Opción 2: Cliente Banamex (solicita y valida `num_cte` real en PostgreSQL).
    """
    print("\n" + "=" * 80)
    print(" AUTENTICACIÓN DE PERFIL - BANAMEX DATA STEWARD (RBAC)")
    print("=" * 80)
    print(" Selecciona tu tipo de usuario:")
    print("   [1] Usuario Universal  (Data Steward — acceso total a SQL y documentación)")
    print("   [2] Cliente Banamex    (Cliente final — acceso exclusivo a tu num_cte)")
    print("=" * 80)

    while True:
        choice = input("\nElige una opción (1 o 2): ").strip().lower()
        if choice in ("1", "opcion 1", "opción 1", "universal"):
            print("\n✔ Sesión iniciada como: USUARIO UNIVERSAL (acceso completo).")
            return "universal", None

        if choice in ("2", "opcion 2", "opción 2", "cliente"):
            print("\nIngresa tu identificador de cliente (`num_cte` en base de datos: entero del 1 al 5000).")
            print("Ejemplos válidos: 1 (PF PREMIUM), 5 (PF PREFERENTE), 50 (PF MASIVO), 3000 (PF MUJER), 5000 (PM)")
            while True:
                raw_id = input("ID de cliente (num_cte) [o 'volver']: ").strip()
                if raw_id.lower() in ("volver", "atras", "atrás"):
                    break

                is_valid, cid, profile = validate_cliente_id(raw_id)
                if is_valid and cid is not None and profile is not None:
                    print(
                        f"\n✔ Sesión iniciada como: CLIENTE BANAMEX (num_cte = {cid} | "
                        f"{profile['tipo_persona']} | {profile['genero_cte']} | "
                        f"Segmento: {profile['segmento_cte']})."
                    )
                    return "cliente", cid

                print("✖ ID inválido. Debe ser un número entero registrado en dim.dim_cliente (1 a 5000). Intenta de nuevo.")
            continue

        print("✖ Opción inválida. Escribe 1 (Usuario Universal) o 2 (Cliente Banamex).")


def run_demo():
    """Ejecuta una batería de pruebas demostrativas cubriendo Universal, Cliente y RBAC."""
    print("\n" + "=" * 80)
    print(" INICIANDO PRUEBAS DEMOSTRATIVAS - PLATAFORMA MULTI-AGENTE BANAMEX (RBAC)")
    print("=" * 80)

    # Prueba 1: Usuario Universal -> acceso total SQL
    ask_data_steward(
        "¿Cuántos clientes tenemos registrados en el segmento PREMIUM?",
        thread_id="demo_universal_1",
        user_role="universal",
        cliente_id=None,
    )

    # Prueba 2: Cliente Final (num_cte = 1) -> consulta permitida de sus propios saldos
    ask_data_steward(
        "¿Cuál es mi saldo actual por producto?",
        thread_id="demo_cliente_1",
        user_role="cliente",
        cliente_id=1,
    )

    # Prueba 3: Cliente Final (num_cte = 1) -> intento bloqueado de ver datos globales
    ask_data_steward(
        "¿Cuántos clientes en total tiene el banco y cuál es el saldo del cliente 5?",
        thread_id="demo_cliente_1_rbac",
        user_role="cliente",
        cliente_id=1,
    )

    # Prueba 4: Multi-Hop (Orquestador -> SQL -> Orquestador -> Chart Tool -> Orquestador)
    ask_data_steward(
        "Dame una gráfica del crecimiento mensual de los saldos de las cuentas de los clientes.",
        thread_id="demo_universal_chart",
        user_role="universal",
        cliente_id=None,
        auto_open_chart=False,
    )


def run_interactive():
    """Modo interactivo en terminal con autenticación RBAC inicial."""
    user_role, cliente_id = authenticate_user()
    thread_id = "interactive_universal" if user_role == "universal" else f"interactive_cliente_{cliente_id}"
    prompt_label = "[Universal]" if user_role == "universal" else f"[Cliente #{cliente_id}]"

    print("\n" + "=" * 80)
    print(" BANAMEX DATA STEWARD - CONSOLA INTERACTIVA (HUB-AND-SPOKE)")
    print(" Escribe tu consulta, 'cambiar' para cambiar de usuario, o 'salir' para terminar.")
    print(" Para cargar documentos (Doc Agent -> RAG): /subir <ruta> [<ruta2> ...]  (usa comillas si la ruta tiene espacios)")
    print("=" * 80)

    while True:
        try:
            q = input(f"\n{prompt_label} > ").strip()
            if not q:
                continue
            if q.lower() in ["salir", "exit", "quit"]:
                print("\nCerrando sesión del Data Steward y limpiando archivos temporales...")
                cleanup_session_files(thread_id)
                print("¡Hasta pronto!")
                break
            if q.lower() in ["cambiar", "logout", "switch"]:
                cleanup_session_files(thread_id)
                user_role, cliente_id = authenticate_user()
                thread_id = "interactive_universal" if user_role == "universal" else f"interactive_cliente_{cliente_id}"
                prompt_label = "[Universal]" if user_role == "universal" else f"[Cliente #{cliente_id}]"
                continue
            if q.lower().startswith("/subir"):
                try:
                    paths = shlex.split(q[len("/subir"):])
                except ValueError as e:
                    print(f"Ruta inválida: {e}")
                    continue
                if not paths:
                    print("Uso: /subir <ruta> [<ruta2> ...]")
                    continue
                missing = [p for p in paths if not Path(p).expanduser().is_file()]
                if missing:
                    print(f"No se encontraron los archivos: {', '.join(missing)}")
                    continue
                pregunta = input(f"{prompt_label} [Pregunta sobre el expediente o trámite, Enter para análisis general] > ").strip()
                ask_data_steward(
                    pregunta,
                    thread_id=thread_id,
                    user_role=user_role,
                    cliente_id=cliente_id,
                    files=paths,
                    auto_open_chart=True,
                )
                continue
            ask_data_steward(
                q,
                thread_id=thread_id,
                user_role=user_role,
                cliente_id=cliente_id,
                auto_open_chart=True,
            )
        except KeyboardInterrupt:
            cleanup_session_files(thread_id)
            print("\nOperación cancelada por el usuario.")
            break
        except Exception as e:
            print(f"\nError en la ejecución: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        run_demo()
    elif len(sys.argv) > 1 and sys.argv[1] in ["--files", "--docs", "--ocr"]:
        # uv run python main.py --files archivo1.pdf [archivo2.png ...] [--pregunta "..."]
        args = sys.argv[2:]
        pregunta = ""
        if "--pregunta" in args:
            i = args.index("--pregunta")
            pregunta = " ".join(args[i + 1:])
            args = args[:i]
        if not args:
            print('Uso: main.py --files <archivo> [<archivo2> ...] [--pregunta "..."]')
            sys.exit(2)
        tid = "files_session_temp"
        try:
            ask_data_steward(pregunta, thread_id=tid, user_role="universal", files=args)
        finally:
            cleanup_session_files(tid)
    else:
        run_interactive()
