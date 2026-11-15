"""HTTP server: REST API under /api and the file-browser web app at /."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from mddb import chat as chat_lib
from mddb import store as store_lib
from mddb import sync as sync_lib

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024
_DOCUMENTS = "/api/documents"
_MARKDOWN_TYPES = ("text/markdown", "text/x-markdown")
_CSP = (
    "default-src 'self'; img-src * data:; object-src 'none';"
    " base-uri 'none'; frame-ancestors 'none'"
)

log = logging.getLogger("mddb")


class ApiError(Exception):
  """An error that is reported to the client as a JSON body."""

  def __init__(self, status: HTTPStatus, message: str):
    super().__init__(message)
    self.status = status
    self.message = message


def _split_tags(values: list[str]) -> list[str]:
  """Flattens repeated and comma-separated tag parameters."""
  return [tag for value in values for tag in value.split(",")]


def _int_param(params: dict[str, list[str]], name: str, default: int) -> int:
  if name not in params:
    return default
  try:
    return int(params[name][-1])
  except ValueError:
    raise ApiError(
        HTTPStatus.BAD_REQUEST, f"{name} must be an integer"
    ) from None


class Handler(BaseHTTPRequestHandler):
  """Request handler; configuration lives on the server object."""

  server_version = "mddb"
  protocol_version = "HTTP/1.1"

  # --- plumbing -----------------------------------------------------------

  def log_message(self, format, *args):  # pylint: disable=redefined-builtin
    log.info("%s %s", self.address_string(), format % args)

  def _send(
      self,
      status: HTTPStatus,
      body: bytes = b"",
      content_type: str | None = None,
      headers: dict[str, str] | None = None,
  ) -> None:
    if not self._body_consumed:
      self._discard_body()
    self.send_response(status)
    if content_type:
      self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Cache-Control", "no-store")
    for name, value in (headers or {}).items():
      self.send_header(name, value)
    self.end_headers()
    if self.command != "HEAD":
      self.wfile.write(body)

  def _send_json(self, status: HTTPStatus, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self._send(status, body, "application/json; charset=utf-8")

  def _discard_body(self) -> None:
    """Reads and drops a request body that the handler did not consume.

    Replying while the client is still sending can reset the connection before
    the client sees the response, and leftover bytes would be parsed as the
    next keep-alive request.
    """
    self._body_consumed = True
    try:
      remaining = int(self.headers.get("Content-Length") or 0)
    except ValueError:
      remaining = -1
    if remaining < 0 or remaining > 4 * self.server.max_body_bytes:
      self.close_connection = True
      return
    while remaining > 0:
      chunk = self.rfile.read(min(remaining, 64 * 1024))
      if not chunk:
        self.close_connection = True
        return
      remaining -= len(chunk)

  def _read_body(self, limit: int | None = None) -> bytes:
    length_header = self.headers.get("Content-Length")
    if length_header is None:
      raise ApiError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
    try:
      length = int(length_header)
    except ValueError:
      raise ApiError(
          HTTPStatus.BAD_REQUEST, "invalid Content-Length"
      ) from None
    if length < 0:
      raise ApiError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
    limit = limit or self.server.max_body_bytes
    if length > limit:
      raise ApiError(
          HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
          f"body is larger than {limit} bytes",
      )
    body = self.rfile.read(length)
    self._body_consumed = True
    return body

  def _decode(self, body: bytes) -> str:
    try:
      return body.decode("utf-8")
    except UnicodeDecodeError:
      raise ApiError(HTTPStatus.BAD_REQUEST, "body must be UTF-8") from None

  def _content_type(self) -> str:
    return self.headers.get("Content-Type", "").split(";")[0].strip().lower()

  def _require_write_access(self) -> None:
    token = self.server.token
    if not token:
      return
    header = self.headers.get("Authorization", "")
    scheme, _, given = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.strip().encode(), token.encode()
    ):
      raise ApiError(
          HTTPStatus.UNAUTHORIZED, "a valid bearer token is required"
      )

  def _dispatch(self) -> None:
    self._body_consumed = False
    try:
      url = urlsplit(self.path)
      path = unquote(url.path)
      params = parse_qs(url.query, keep_blank_values=False)
      if path == "/api" or path.startswith("/api/"):
        self._handle_api(path, params)
      elif self.command in ("GET", "HEAD"):
        self._handle_static(path)
      else:
        raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
    except ApiError as e:
      self._send_json(e.status, {"error": e.message})
    except store_lib.NotFoundError as e:
      self._send_json(HTTPStatus.NOT_FOUND, {"error": str(e)})
    except store_lib.StoreError as e:
      self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
    except (BrokenPipeError, ConnectionResetError):
      self.close_connection = True
    except Exception:  # pylint: disable=broad-except
      log.exception("unhandled error for %s %s", self.command, self.path)
      self.close_connection = True
      self._send_json(
          HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"}
      )

  do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _dispatch

  # --- API ----------------------------------------------------------------

  def _handle_api(self, path: str, params: dict[str, list[str]]) -> None:
    method = "GET" if self.command == "HEAD" else self.command
    db: store_lib.Store = self.server.store

    if path == "/api/health" and method == "GET":
      self._send_json(HTTPStatus.OK, {"status": "ok"})
    elif path == "/api/tags" and method == "GET":
      self._send_json(HTTPStatus.OK, {"tags": db.tags()})
    elif path == _DOCUMENTS and method == "GET":
      documents, total = db.list(
          query=(params.get("q") or [None])[-1],
          tags=_split_tags(params.get("tag", []) + params.get("tags", [])),
          prefix=(params.get("prefix") or [None])[-1],
          limit=_int_param(params, "limit", store_lib.DEFAULT_LIMIT),
          offset=_int_param(params, "offset", 0),
      )
      self._send_json(
          HTTPStatus.OK,
          {"documents": [d.to_dict() for d in documents], "total": total},
      )
    elif path == _DOCUMENTS and method == "POST":
      self._require_write_access()
      payload = self._read_json()
      if "path" not in payload:
        raise ApiError(HTTPStatus.BAD_REQUEST, '"path" is required')
      self._put(payload["path"], payload)
    elif path.startswith(_DOCUMENTS + "/"):
      doc_path = path[len(_DOCUMENTS) + 1 :]
      if method == "GET":
        self._get(doc_path, params)
      elif method == "PUT":
        self._require_write_access()
        if self._content_type() == "application/json":
          self._put(doc_path, self._read_json())
        elif self._content_type() in _MARKDOWN_TYPES:
          payload = {"content": self._decode(self._read_body())}
          if "tag" in params or "tags" in params:
            payload["tags"] = _split_tags(
                params.get("tag", []) + params.get("tags", [])
            )
          self._put(doc_path, payload)
        else:
          raise ApiError(
              HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
              "Content-Type must be application/json or text/markdown",
          )
      elif method == "DELETE":
        self._require_write_access()
        db.delete(doc_path)
        self._send(HTTPStatus.NO_CONTENT)
      else:
        raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
    elif path == "/api/chat" and method == "GET":
      chat = self.server.chat
      self._send_json(
          HTTPStatus.OK,
          {
              "enabled": chat is not None,
              "model": chat.model if chat else None,
              "token_required": bool(self.server.token),
          },
      )
    elif path == "/api/chat" and method == "POST":
      # Chat spends the owner's model quota, so it is guarded like a write.
      self._require_write_access()
      self._chat()
    elif path == "/api/sync/changes" and method == "GET":
      self._send_json(
          HTTPStatus.OK,
          db.changes(
              since=_int_param(params, "since", 0),
              limit=_int_param(params, "limit", store_lib.DEFAULT_CHANGES_LIMIT),
              exclude_origin=(params.get("exclude_origin") or [None])[-1],
          ),
      )
    elif path == "/api/sync/changes" and method == "POST":
      self._require_write_access()
      # A single large document plus JSON escaping can exceed the normal limit.
      payload = self._read_json(limit=4 * self.server.max_body_bytes)
      origin = payload.get("instance_id")
      if not isinstance(origin, str) or not origin or len(origin) > 64:
        raise ApiError(HTTPStatus.BAD_REQUEST, '"instance_id" is required')
      result = db.apply_changes(payload.get("changes"), origin=origin)
      self._send_json(HTTPStatus.OK, {"instance_id": db.instance_id, **result})
    elif path == "/api/sync" and method == "GET":
      syncer = self.server.syncer
      self._send_json(
          HTTPStatus.OK,
          {"configured": syncer is not None, **(syncer.status() if syncer else {})},
      )
    elif path == "/api/sync" and method == "POST":
      self._require_write_access()
      syncer = self.server.syncer
      if syncer is None:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "this server was started without --sync-url",
        )
      try:
        result = syncer.run()
      except sync_lib.SyncError as e:
        raise ApiError(HTTPStatus.BAD_GATEWAY, f"sync failed: {e}") from None
      self._send_json(HTTPStatus.OK, {"remote": syncer.remote_url, **result})
    elif path in (
        "/api/health", "/api/tags", "/api/chat", "/api/sync",
        "/api/sync/changes", _DOCUMENTS,
    ):
      raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
    else:
      raise ApiError(HTTPStatus.NOT_FOUND, "no such API endpoint")

  def _chat(self) -> None:
    """Streams a model answer as newline-delimited JSON events."""
    chat = self.server.chat
    if chat is None:
      raise ApiError(
          HTTPStatus.SERVICE_UNAVAILABLE,
          "chat is not enabled: start the server with GEMINI_API_KEY set",
      )
    payload = self._read_json()
    try:
      request_body, context = chat.prepare(
          self.server.store, payload.get("messages"), payload.get("paths")
      )
    except chat_lib.ChatError as e:
      raise ApiError(HTTPStatus.BAD_REQUEST, str(e)) from None

    # The length is not known in advance, so the body ends when the
    # connection closes.
    self.close_connection = True
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Connection", "close")
    self.end_headers()

    def emit(event: dict) -> None:
      line = json.dumps(event, ensure_ascii=False) + "\n"
      self.wfile.write(line.encode("utf-8"))
      self.wfile.flush()

    emit({"type": "context", "model": chat.model, **context})
    try:
      for event in chat.stream(request_body):
        emit(event)
      emit({"type": "done"})
    except (BrokenPipeError, ConnectionResetError):
      return  # The client stopped listening.
    except Exception:  # pylint: disable=broad-except
      log.exception("chat failed")
      try:
        emit({"type": "error", "message": "internal server error"})
      except OSError:
        pass

  def _read_json(self, limit: int | None = None) -> dict:
    if self._content_type() != "application/json":
      raise ApiError(
          HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
          "Content-Type must be application/json",
      )
    try:
      payload = json.loads(self._decode(self._read_body(limit)))
    except json.JSONDecodeError as e:
      raise ApiError(HTTPStatus.BAD_REQUEST, f"invalid JSON: {e}") from None
    if not isinstance(payload, dict):
      raise ApiError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
    return payload

  def _put(self, doc_path, payload: dict) -> None:
    content = payload.get("content")
    if not isinstance(content, str):
      raise ApiError(
          HTTPStatus.BAD_REQUEST, '"content" is required and must be a string'
      )
    document, created = self.server.store.put(
        doc_path, content, payload.get("tags")
    )
    self._send_json(
        HTTPStatus.CREATED if created else HTTPStatus.OK, document.to_dict()
    )

  def _get(self, doc_path: str, params: dict[str, list[str]]) -> None:
    document = self.server.store.get(doc_path)
    wants_raw = (params.get("raw") or ["0"])[-1].lower() in ("1", "true")
    accept = self.headers.get("Accept", "")
    if wants_raw or any(t in accept for t in _MARKDOWN_TYPES):
      self._send(
          HTTPStatus.OK,
          document.content.encode("utf-8"),
          "text/markdown; charset=utf-8",
          # Never let a browser render stored content as a page of this site.
          {"Content-Security-Policy": "sandbox; default-src 'none'"},
      )
    else:
      self._send_json(HTTPStatus.OK, document.to_dict())

  # --- web app ------------------------------------------------------------

  def _handle_static(self, path: str) -> None:
    relative = "index.html" if path == "/" else path.lstrip("/")
    root = WEB_DIR.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or not target.is_file():
      raise ApiError(HTTPStatus.NOT_FOUND, "not found")
    content_type = mimetypes.guess_type(target.name)[0] or (
        "application/octet-stream"
    )
    if content_type.startswith("text/") or content_type.endswith("javascript"):
      content_type += "; charset=utf-8"
    self._send(
        HTTPStatus.OK,
        target.read_bytes(),
        content_type,
        {"Content-Security-Policy": _CSP, "Referrer-Policy": "no-referrer"},
    )


class Server(ThreadingHTTPServer):
  """Threaded HTTP server holding the store and configuration."""

  daemon_threads = True
  allow_reuse_address = True

  def __init__(
      self,
      address: tuple[str, int],
      store: store_lib.Store,
      token: str | None = None,
      max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
      syncer: sync_lib.Syncer | None = None,
      chat: chat_lib.Chat | None = None,
  ):
    self.store = store
    self.syncer = syncer
    self.chat = chat
    self.token = token or None
    self.max_body_bytes = max_body_bytes
    if ":" in address[0]:
      self.address_family = socket.AF_INET6
    super().__init__(address, Handler)

  def server_bind(self) -> None:
    if self.address_family == socket.AF_INET6:
      # Accept IPv4 connections too when listening on "::".
      self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    super().server_bind()
