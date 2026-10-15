"""Download-path tests against a local stub server serving synthetic bytes."""

import hashlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from musicdiscovery import ingest as ingest_module
from musicdiscovery.ingest import ARCHIVE_NAME, IngestError, fetch_archive

PAYLOAD = b"synthetic archive bytes" * 100
SHA1 = hashlib.sha1(PAYLOAD).hexdigest()


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ingest_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ingest_module, "DOWNLOAD_TIMEOUT_SECONDS", 0.5)


@pytest.fixture
def serve():
    """Start a stub server; ``script`` is a list of per-request handlers."""
    servers = []

    def start(script):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                script[min(len(calls), len(script)) - 1](self)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}/a.zip", calls

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def ok(handler, body=PAYLOAD):
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def status(code):
    def respond(handler):
        handler.send_response(code)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def stall(handler):
    handler.connection.settimeout(5)
    try:
        handler.connection.recv(1)
    except (TimeoutError, OSError):
        pass


def assert_clean(cache_dir):
    assert not (cache_dir / ARCHIVE_NAME).exists()
    assert not list(cache_dir.glob("*.part"))


def test_download_succeeds(tmp_path, serve):
    url, calls = serve([ok])
    path = fetch_archive(url, SHA1, tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert len(calls) == 1
    assert not list(tmp_path.glob("*.part"))


def test_stalled_server_times_out(tmp_path, serve):
    url, calls = serve([stall])
    with pytest.raises(IngestError, match="after 3 attempt") as info:
        fetch_archive(url, SHA1, tmp_path)
    assert url in str(info.value)
    assert len(calls) == 3
    assert_clean(tmp_path)


def test_503_then_200_succeeds(tmp_path, serve):
    url, calls = serve([status(503), ok])
    assert fetch_archive(url, SHA1, tmp_path).read_bytes() == PAYLOAD
    assert len(calls) == 2


def test_persistent_503_gives_up(tmp_path, serve):
    url, calls = serve([status(503)])
    with pytest.raises(IngestError, match="after 3 attempt"):
        fetch_archive(url, SHA1, tmp_path)
    assert len(calls) == 3
    assert_clean(tmp_path)


def test_404_is_not_retried(tmp_path, serve):
    url, calls = serve([status(404)])
    with pytest.raises(IngestError, match="after 1 attempt") as info:
        fetch_archive(url, SHA1, tmp_path)
    assert "404" in str(info.value)
    assert len(calls) == 1
    assert_clean(tmp_path)


def truncated(handler):
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(PAYLOAD)))
    handler.end_headers()
    handler.wfile.write(PAYLOAD[:50])
    handler.connection.shutdown(socket.SHUT_RDWR)


@pytest.fixture
def responses(monkeypatch, tmp_path):
    """Track real responses and verify cleanup before each new request."""
    opened = []
    urlopen = ingest_module.urllib.request.urlopen

    def request(*args, **kwargs):
        assert all(response.closed for response in opened)
        assert_clean(tmp_path)
        response = urlopen(*args, **kwargs)
        opened.append(response)
        return response

    monkeypatch.setattr(ingest_module.urllib.request, "urlopen", request)
    return opened


@pytest.mark.parametrize("chunk_bytes", [32, ingest_module._CHUNK_BYTES])
def test_truncated_then_complete_succeeds(
    tmp_path, serve, responses, monkeypatch, chunk_bytes
):
    monkeypatch.setattr(ingest_module, "_CHUNK_BYTES", chunk_bytes)
    url, calls = serve([truncated, ok])
    path = fetch_archive(url, SHA1, tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert len(calls) == 2
    assert len(responses) == 2
    assert all(response.closed for response in responses)
    assert not list(tmp_path.glob("*.part"))


def test_persistent_truncation_gives_up(tmp_path, serve, responses):
    url, calls = serve([truncated])
    with pytest.raises(IngestError, match="incomplete HTTP response") as info:
        fetch_archive(url, SHA1, tmp_path)
    message = str(info.value)
    assert url in message
    assert f"after {ingest_module.DOWNLOAD_ATTEMPTS} attempt(s)" in message
    assert f"received 50 of {len(PAYLOAD)} bytes" in message
    assert len(calls) == ingest_module.DOWNLOAD_ATTEMPTS
    assert len(responses) == ingest_module.DOWNLOAD_ATTEMPTS
    assert all(response.closed for response in responses)
    assert isinstance(info.value.__cause__, OSError)
    assert_clean(tmp_path)


def test_complete_incorrect_body_is_not_retried(tmp_path, serve, responses):
    def incorrect(handler):
        ok(handler, body=b"incorrect archive")

    url, calls = serve([incorrect, ok])
    with pytest.raises(IngestError, match="checksum mismatch"):
        fetch_archive(url, SHA1, tmp_path)
    assert len(calls) == 1
    assert responses[0].closed
    assert_clean(tmp_path)


def test_body_without_content_length_succeeds(tmp_path, serve):
    def unbounded(handler):
        handler.send_response(200)
        handler.end_headers()
        handler.wfile.write(PAYLOAD)

    url, calls = serve([unbounded])
    assert fetch_archive(url, SHA1, tmp_path).read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_chunked_body_succeeds(tmp_path, serve):
    def chunked(handler):
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.wfile.write(
            f"{len(PAYLOAD):x}\r\n".encode() + PAYLOAD + b"\r\n0\r\n\r\n"
        )

    url, calls = serve([chunked])
    assert fetch_archive(url, SHA1, tmp_path).read_bytes() == PAYLOAD
    assert len(calls) == 1


def test_stale_part_file_is_cleaned(tmp_path, serve):
    (tmp_path / "fma_metadata.part").write_bytes(b"stale" * 1000)
    url, _ = serve([ok])
    path = fetch_archive(url, SHA1, tmp_path)
    assert path.read_bytes() == PAYLOAD
    assert not list(tmp_path.glob("*.part"))


def test_stale_part_file_removed_on_failure(tmp_path, serve):
    (tmp_path / "fma_metadata.part").write_bytes(b"stale")
    url, _ = serve([status(404)])
    with pytest.raises(IngestError):
        fetch_archive(url, SHA1, tmp_path)
    assert_clean(tmp_path)
