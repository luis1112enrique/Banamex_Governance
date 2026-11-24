"""Local-only HTTP bridge for the banking prototype and existing LangGraph."""

import hmac
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api import authorization as auth
from api.services import assistant, banking, uploads


app = FastAPI(title="Banamex Data Steward AI · Prototipo", docs_url=None, redoc_url=None)
COOKIE = "banamex_test_session"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    if not request.client or request.client.host not in LOOPBACK:
        return error_response(403, "local_only", "Este prototipo solo admite acceso local.")
    if request.url.hostname not in LOOPBACK:
        return error_response(403, "invalid_host", "El servidor debe utilizar una dirección local.")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        try:
            origin_port = parsed.port
        except ValueError:
            origin_port = None
        if parsed.hostname not in LOOPBACK or parsed.scheme != "http" or origin_port not in {4173, 5173, 8000}:
            return error_response(403, "invalid_origin", "El origen de la solicitud no está autorizado.")
    try:
        body_size = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return error_response(400, "invalid_request", "El tamaño de la solicitud no es válido.")
    body_limit = uploads.MAX_REQUEST_BYTES if request.method == "POST" and request.url.path == uploads.DOCUMENT_PATH else 24_000
    if body_size > body_limit:
        return error_response(413, "request_too_large", "La solicitud excede el tamaño permitido.")
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.exception_handler(auth.APIError)
async def handle_api_error(_: Request, exc: auth.APIError):
    return error_response(exc.status_code, exc.code, exc.message)


@app.exception_handler(banking.BankingUnavailable)
async def handle_banking_error(_: Request, exc: banking.BankingUnavailable):
    return error_response(exc.status_code, exc.code, str(exc))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError):
    return error_response(422, "invalid_request", "La solicitud contiene campos inválidos o no permitidos.")


def principal(request: Request) -> auth.Principal:
    return auth.require_session(request.cookies.get(COOKIE))


def mutation_principal(request: Request, session: auth.Principal = Depends(principal)) -> auth.Principal:
    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf_token):
        raise auth.APIError(403, "csrf", "La solicitud no pertenece a la sesión activa. Actualiza la página.")
    return session


def client_principal(request: Request, session: auth.Principal = Depends(principal)) -> auth.Principal:
    auth.ensure_principal(session, "banking:read")
    if session.portal != "cliente" or session.cliente_id is None:
        raise auth.APIError(403, "client_scope", "La sesión no tiene un cliente autorizado.")
    if any(key in request.query_params for key in ("cliente_id", "num_cte", "user_role", "profile")):
        raise auth.APIError(400, "server_identity", "La identidad del cliente se determina únicamente en el servidor.")
    return session


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    portal: str = Field(pattern="^(cliente|institucional)$")


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(min_length=36, max_length=36)
    question: str = Field(min_length=1, max_length=4000)


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    result = auth.login(body.username, body.password, body.portal)
    previous = request.cookies.get(COOKIE)
    if previous:
        try:
            old = auth.require_session(previous)
            assistant.clear_session(old.session_id)
        except auth.APIError:
            pass
        auth.logout(previous)
    response.set_cookie(COOKIE, result.token, max_age=auth.SESSION_TTL, httponly=True, samesite="strict", secure=False, path="/api")
    return auth.public_session(result.principal)


@app.get("/api/session")
def session_info(session: auth.Principal = Depends(principal)):
    return auth.public_session(session)


@app.post("/api/auth/logout")
def logout(request: Request, response: Response, session: auth.Principal = Depends(mutation_principal)):
    assistant.clear_session(session.session_id)
    auth.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/api")
    return {"ok": True}


@app.get("/api/me/summary")
def summary(session: auth.Principal = Depends(client_principal)):
    return banking.get_summary(session.cliente_id)


@app.get("/api/me/accounts")
def accounts(session: auth.Principal = Depends(client_principal)):
    return banking.get_accounts(session.cliente_id)


@app.get("/api/me/products")
def products(session: auth.Principal = Depends(client_principal)):
    return banking.get_products(session.cliente_id)


@app.get("/api/me/transactions")
def transactions(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), session: auth.Principal = Depends(client_principal)):
    return banking.get_transactions(session.cliente_id, limit=limit, offset=offset)


@app.get("/api/institution/summary")
def institution_summary(session: auth.Principal = Depends(principal)):
    auth.ensure_principal(session, "institution:read")
    return banking.get_institution_summary()


@app.post("/api/conversations")
def conversation(session: auth.Principal = Depends(mutation_principal)):
    return assistant.create_conversation(session)


@app.post("/api/assistant/chat")
def chat(body: ChatRequest, session: auth.Principal = Depends(mutation_principal)):
    return assistant.ask(session, body.conversation_id, body.question)


@app.post(uploads.DOCUMENT_PATH)
async def documents(request: Request, session: auth.Principal = Depends(mutation_principal)):
    return await uploads.receive_documents(request, session)


@app.get("/api/charts/{chart_id}")
def chart(chart_id: str, session: auth.Principal = Depends(principal)):
    path = assistant.get_chart(session, chart_id)
    return FileResponse(path, media_type="image/png", headers={"Content-Disposition": "inline"})


@app.get("/api/health")
def health(response: Response):
    try:
        auth.authorized_config()
        banking.check_connection()
    except (auth.APIError, banking.BankingUnavailable) as exc:
        response.status_code = 503
        return {"status": "degraded", "api": "ok", "postgres": "unavailable", "code": exc.code}
    return {"status": "ok", "api": "ok", "postgres": "ok", "assistant": "not_probed"}
