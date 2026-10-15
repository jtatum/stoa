"""Tests for request ID middleware."""

import asyncio
import json
import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from stoa.logging_config import JSONFormatter
from stoa.main import app
from stoa.request_id import RequestIDMiddleware, get_request_id, request_id_var

client = TestClient(app)


def test_generates_request_id_if_missing() -> None:
    """Request ID is generated if not provided."""
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert len(response.headers["X-Request-ID"]) == 36  # UUID format


@pytest.mark.parametrize("custom_id", ["a", "test-request-123", "AZaz09._-", "a" * 128])
def test_propagates_request_id_if_provided(custom_id: str) -> None:
    """Request ID from client is propagated in response."""
    response = client.get("/health", headers={"X-Request-ID": custom_id})
    assert response.headers["X-Request-ID"] == custom_id


@pytest.mark.parametrize(
    "custom_id",
    [
        b"",
        b"a" * 129,
        b"a" * 10000,
        b" leading-space",
        b"trailing-space ",
        b"embedded space",
        b"tab\tcharacter",
        b"carriage\rreturn",
        b"newline\n",
        b"injected\r\nX-Forged: true",
        b"null\x00byte",
        b"delete\x7fcharacter",
        b"escape\x1bcharacter",
        b"non-ascii-\xe9",
        b"comma,separated",
        b"slash/value",
        b'quote"value',
    ],
    ids=[
        "empty",
        "over-limit",
        "very-long",
        "leading-space",
        "trailing-space",
        "space",
        "tab",
        "cr",
        "trailing-lf",
        "crlf-injection",
        "nul",
        "del",
        "escape",
        "non-ascii",
        "comma",
        "slash",
        "quote",
    ],
)
def test_replaces_invalid_request_id(custom_id: bytes) -> None:
    """Untrusted header values are replaced with UUIDs, never echoed back."""
    response = client.get("/health", headers={"X-Request-ID": custom_id})

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id.encode("ascii") != custom_id
    assert str(UUID(request_id, version=4)) == request_id


def test_different_requests_get_different_ids() -> None:
    """Each request gets a unique ID."""
    response1 = client.get("/health")
    response2 = client.get("/health")
    assert response1.headers["X-Request-ID"] != response2.headers["X-Request-ID"]


@pytest.mark.anyio
async def test_invalid_request_id_is_replaced_in_logs_and_response() -> None:
    """The same fresh ID is used by downstream logs and the response header."""
    request = Request({"type": "http", "headers": [(b"x-request-id", b"forged\nlog-entry")]})
    logged_ids = []

    async def call_next(request: Request) -> Response:
        record = logging.LogRecord("test", logging.INFO, "", 0, "Handled request", (), None)
        logged_ids.append(json.loads(JSONFormatter().format(record))["request_id"])
        return Response()

    middleware = RequestIDMiddleware(app)
    responses = [await middleware.dispatch(request, call_next) for _ in range(2)]
    response_ids = [response.headers["X-Request-ID"] for response in responses]

    assert logged_ids == response_ids
    assert len(set(response_ids)) == 2
    for request_id in response_ids:
        assert str(UUID(request_id, version=4)) == request_id


@pytest.mark.anyio
@pytest.mark.parametrize("previous_id", ["", "outer-request"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_restores_context_after_request(previous_id: str, outcome: str) -> None:
    """Request context is restored even when downstream code fails or is cancelled."""
    request = Request({"type": "http", "headers": [(b"x-request-id", b"inner-request")]})
    error = asyncio.CancelledError() if outcome == "cancelled" else RuntimeError("failed")

    async def call_next(request: Request) -> Response:
        assert get_request_id() == "inner-request"
        if outcome != "success":
            raise error
        return Response()

    token = request_id_var.set(previous_id)
    try:
        middleware = RequestIDMiddleware(app)
        if outcome == "success":
            response = await middleware.dispatch(request, call_next)
            assert response.headers["X-Request-ID"] == "inner-request"
        else:
            with pytest.raises(type(error)) as raised:
                await middleware.dispatch(request, call_next)
            assert raised.value is error
        assert get_request_id() == previous_id
    finally:
        request_id_var.reset(token)
