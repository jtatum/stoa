"""Request ID middleware for correlation across logs and responses."""

import re
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")


def get_request_id() -> str:
    """Get the current request ID from context."""
    return request_id_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Correlate logs and responses with a bounded, header-safe X-Request-ID.

    Client IDs must be 1–128 ASCII letters, digits, dots, underscores, or hyphens.
    Missing or invalid IDs are replaced with a fresh UUID.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Validate the request ID and scope its context to downstream handling."""
        request_id = request.headers.get("X-Request-ID", "")
        if not _REQUEST_ID_PATTERN.fullmatch(request_id):
            request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)

        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)
