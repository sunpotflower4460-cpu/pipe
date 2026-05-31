from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.responses import error, plain_text
from app.ingest import router as ingest_router

app = FastAPI()
app.include_router(ingest_router)


# ---------------------------------------------------------------------------
# Exception handlers — keep every error response text/plain; charset=utf-8
# ---------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> PlainTextResponse:
    messages = {
        400: "invalid request",
        401: "unauthorized",
        403: "invalid or expired token",
        404: "not found",
        405: "method not allowed",
        422: "invalid request",
        500: "internal server error",
    }
    if isinstance(exc.detail, str):
        detail = exc.detail
    else:
        detail = messages.get(exc.status_code, "error")
    return error(detail, exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> PlainTextResponse:
    return error("invalid request", 422)


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
    return error("internal server error", 500)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> PlainTextResponse:
    return plain_text("ok")
