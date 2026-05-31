from fastapi.responses import PlainTextResponse


def plain_text(content: str, status_code: int = 200) -> PlainTextResponse:
    """Return a text/plain; charset=utf-8 response."""
    return PlainTextResponse(content=content, status_code=status_code)


def error(message: str, status_code: int) -> PlainTextResponse:
    """Return a plain-text error response.

    Example body: "ERROR: file not found"
    """
    return PlainTextResponse(content=f"ERROR: {message}", status_code=status_code)
