"""Receive browser attachments as private, short-lived local paths."""

import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException

from api.authorization import APIError, Principal, ensure_principal
from api.services import assistant


MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_TOTAL_BYTES + 64 * 1024
DOCUMENT_PATH = "/api/assistant/documents"
SIGNATURES = {".pdf": b"%PDF-", ".png": b"\x89PNG\r\n\x1a\n", ".jpg": b"\xff\xd8\xff", ".jpeg": b"\xff\xd8\xff"}


async def receive_documents(request: Request, principal: Principal) -> dict:
    ensure_principal(principal, "assistant:use")
    # Bound chunked bodies too, before the multipart parser allocates its spools.
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            raise APIError(413, "upload_too_large", "Los documentos superan el límite total de 20 MiB.")
        body.extend(chunk)

    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    bounded_request = Request(request.scope, receive=receive)
    try:
        async with bounded_request.form(max_files=MAX_FILES, max_fields=2, max_part_size=16_000) as form:
            body.clear()
            if set(form) - {"conversation_id", "question", "files"}:
                raise APIError(400, "invalid_upload_fields", "La carga contiene campos no permitidos.")
            ids, questions = form.getlist("conversation_id"), form.getlist("question")
            if len(ids) != 1 or not isinstance(ids[0], str) or len(ids[0]) != 36:
                raise APIError(400, "invalid_conversation", "Selecciona una conversación válida.")
            if len(questions) > 1 or (questions and not isinstance(questions[0], str)):
                raise APIError(400, "invalid_question", "La pregunta no es válida.")
            question = questions[0] if questions else ""
            if len(question) > 4000:
                raise APIError(400, "invalid_question", "La pregunta admite hasta 4,000 caracteres.")
            conversation_id = ids[0]
            assistant._owned_conversation(principal, conversation_id)
            files = form.getlist("files")
            if not 1 <= len(files) <= MAX_FILES or any(not isinstance(item, UploadFile) for item in files):
                raise APIError(400, "invalid_files", "Adjunta entre 1 y 5 documentos PDF, PNG o JPG.")
            paths = []
            total = 0
            # Only HTTP transport temporaries; the backend owns inputs/{thread_id}.
            with TemporaryDirectory(prefix=f"banamex-upload-{conversation_id}-") as directory:
                for index, upload in enumerate(files):
                    name = Path((upload.filename or "").replace("\\", "/")).name
                    extension = Path(name).suffix.lower()
                    if extension not in SIGNATURES:
                        raise APIError(415, "unsupported_document", "Solo se admiten documentos PDF, PNG, JPG y JPEG.")
                    header = await upload.read(8)
                    if not header:
                        raise APIError(400, "empty_document", "Uno de los documentos está vacío.")
                    if not header.startswith(SIGNATURES[extension]):
                        raise APIError(415, "invalid_document_type", "El contenido de un documento no coincide con su extensión.")
                    await upload.seek(0)
                    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[-160:]
                    path = Path(directory) / f"{index + 1:02d}_{safe_name}"
                    size = 0
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as target:
                        while chunk := await upload.read(64 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                                raise APIError(413, "upload_too_large", "Máximo 10 MiB por documento y 20 MiB en total.")
                            target.write(chunk)
                    paths.append(str(path))
                return await run_in_threadpool(assistant.ask, principal, conversation_id, question, files=paths)
    except HTTPException as exc:
        raise APIError(400, "invalid_multipart", "Carga inválida: adjunta hasta 5 documentos y una pregunta de hasta 4,000 caracteres.") from exc
    except OSError as exc:
        raise APIError(503, "upload_storage", "No fue posible recibir los documentos. Inténtalo nuevamente.") from exc
