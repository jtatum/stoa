"""Tests for Python client library."""

# Import from clients/python (add to path)
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, "clients/python")

from herd_client import HerdClient


def test_client_requires_api_key() -> None:
    """Should require API key in constructor."""
    with pytest.raises(ValueError, match="api_key is required"):
        HerdClient(api_key="")


def test_client_get_participating(client: HerdClient) -> None:
    """Should fetch participating threads."""
    # Mock response
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {
            "threads": [
                {
                    "thread_id": 1,
                    "subject": "Test",
                    "callback_flag": True,
                    "new_replies_since": 2,
                }
            ]
        }

        threads = client.get_participating()
        assert len(threads) == 1
        assert threads[0]["thread_id"] == 1
        assert threads[0]["callback_flag"] is True


def test_client_get_post(client: HerdClient) -> None:
    """Should fetch full post by ID."""
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {
            "id": 1,
            "subject": "Test Post",
            "body_markdown": "Content here",
        }

        post = client.get_post(1)
        assert post["id"] == 1
        assert post["subject"] == "Test Post"


def test_client_poll_participating(client: HerdClient) -> None:
    """Should poll participating threads with interval."""
    with patch("requests.Session.request") as mock_request:
        mock_request.return_value.status_code = 200
        mock_request.return_value.json.return_value = {"threads": [{"thread_id": 1}]}

        # Poll once (generator)
        gen = client.poll_participating(interval=1, max_polls=1)
        threads = next(gen)
        assert len(threads) == 1


def test_client_handles_rate_limit(client: HerdClient) -> None:
    """Should retry on 429 with exponential backoff."""
    with patch("requests.Session.request") as mock_request, patch("time.sleep") as mock_sleep:
        # First call: rate limited, second call: success
        mock_request.return_value.status_code = 429
        mock_request.return_value.headers = {"Retry-After": "2"}

        def side_effect(*args, **kwargs):  # type: ignore[no-untyped-def]
            if mock_request.call_count == 1:
                resp = MagicMock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "2"}
                return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"threads": []}
            return resp

        mock_request.side_effect = side_effect

        threads = client.get_participating()
        assert mock_sleep.called
        assert threads == []
        assert all(call.kwargs["allow_redirects"] is False for call in mock_request.call_args_list)


@contextmanager
def serve_http(handler: type[BaseHTTPRequestHandler]) -> Iterator[str]:
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("cross_origin", [False, True])
@pytest.mark.parametrize("method", ["get_post", "get_participating"])
def test_client_rejects_redirects(status: int, cross_origin: bool, method: str) -> None:
    """No redirected request (or API key) may reach a second path or origin."""
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            received.append((self.path, self.headers.get("X-API-Key")))
            if self.path == "/redirected":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"id": 1, "threads": []}')
            else:
                self.send_response(status)
                self.send_header("Location", location)
                self.end_headers()
                # Valid JSON must not make a 3xx response look successful.
                self.wfile.write(b'{"id": 1, "threads": []}')

    with serve_http(Handler) as target, serve_http(Handler) as source:
        location = f"{target}/redirected" if cross_origin else "/redirected"
        client = HerdClient(api_key="test-key", base_url=source)
        client.session.trust_env = False
        try:
            with pytest.raises(requests.HTTPError, match=f"HTTP {status}") as error:
                if method == "get_post":
                    client.get_post(1)
                else:
                    client.get_participating()
            assert error.value.response.status_code == status
            path = "/api/posts/1" if method == "get_post" else "/api/posts/participating"
            assert received == [(path, "test-key")]
        finally:
            client.session.close()


@pytest.mark.parametrize("status", [300, 304, 305, 306, 399])
def test_client_rejects_other_3xx_responses(client: HerdClient, status: int) -> None:
    response = requests.Response()
    response.status_code = status
    response._content = b"{}"
    response._content_consumed = True
    with patch.object(client.session, "request", return_value=response):
        with pytest.raises(requests.HTTPError, match=f"HTTP {status}"):
            client.get_post(1)


@pytest.fixture
def client() -> HerdClient:
    """Create test client."""
    return HerdClient(api_key="test_key", base_url="http://localhost:8000")
