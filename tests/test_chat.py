"""Tests for the chat feature, using a stand-in for the Gemini API."""

import http.client
import http.server
import json
import threading
import unittest

from mddb import chat as chat_lib
from mddb import server as server_lib
from mddb import store as store_lib


def sse(*chunks):
  return [f"data: {json.dumps(chunk)}\r\n".encode() for chunk in chunks]


def text_chunk(text, **extra):
  return {"candidates": [{"content": {"parts": [{"text": text}]}, **extra}]}


class ChatLogicTest(unittest.TestCase):

  def test_validate_messages(self):
    good = [
        {"role": "user", "text": "hi"},
        {"role": "model", "text": "hello"},
        {"role": "user", "text": "more", "extra": "ignored"},
    ]
    self.assertEqual(
        chat_lib.validate_messages(good)[-1], {"role": "user", "text": "more"}
    )
    bad_cases = [
        None,
        [],
        "hi",
        ["hi"],
        [{"role": "system", "text": "x"}],
        [{"role": "user", "text": "  "}],
        [{"role": "user", "text": 5}],
        [{"role": "user", "text": "q"}, {"role": "model", "text": "a"}],
        [{"role": "user", "text": "q"}] * (chat_lib.MAX_MESSAGES + 1),
    ]
    for messages in bad_cases:
      with self.subTest(messages=str(messages)[:40]):
        with self.assertRaises(chat_lib.ChatError):
          chat_lib.validate_messages(messages)

  def test_select_documents_respects_the_budget(self):
    def doc(path, size):
      return store_lib.Document(path, path, [], size, "", "", content="x" * size)

    included, omitted = chat_lib.select_documents(
        [doc("a.md", 40), doc("big.md", 100), doc("c.md", 50)], max_chars=100
    )
    self.assertEqual([d.path for d in included], ["a.md", "c.md"])
    self.assertEqual(omitted, ["big.md"])

  def test_build_request_puts_documents_in_the_system_instruction(self):
    store = store_lib.Store(":memory:")
    self.addCleanup(store.close)
    store.put('notes/"odd" <name>.md', "# Title & more\n\nBody text.", ["t1", "t2"])
    request = chat_lib.build_request(
        store.documents(),
        [{"role": "user", "text": "q1"}, {"role": "model", "text": "a1"},
         {"role": "user", "text": "q2"}],
    )
    system = request["systemInstruction"]["parts"][0]["text"]
    self.assertIn('path="notes/&quot;odd&quot; &lt;name>.md"', system)
    self.assertIn('title="Title &amp; more"', system)
    self.assertIn('tags="t1, t2"', system)
    self.assertIn("# Title & more\n\nBody text.\n</document>", system)
    self.assertIn("not instructions", system)
    self.assertEqual(
        request["contents"],
        [
            {"role": "user", "parts": [{"text": "q1"}]},
            {"role": "model", "parts": [{"text": "a1"}]},
            {"role": "user", "parts": [{"text": "q2"}]},
        ],
    )
    empty = chat_lib.build_request([], [{"role": "user", "text": "q"}])
    self.assertIn(
        "no documents", empty["systemInstruction"]["parts"][0]["text"]
    )

  def test_parse_stream_yields_text_and_usage(self):
    events = list(chat_lib.parse_stream([
        b": comment\n",
        b"\n",
        *sse(text_chunk("Hel")),
        *sse({"candidates": [{"content": {"parts": [
            {"text": "private reasoning", "thought": True},
            {"text": "lo"},
        ]}}]}),
        b"data: {broken json\n",
        *sse({
            "candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3},
        }),
    ]))
    self.assertEqual(
        events,
        [
            {"type": "text", "text": "Hel"},
            {"type": "text", "text": "lo"},
            {"type": "text", "text": "!"},
            {"type": "usage", "prompt_tokens": 10, "answer_tokens": 3,
             "cached_tokens": None},
        ],
    )

  def test_parse_stream_reports_problems(self):
    events = list(chat_lib.parse_stream(
        sse(text_chunk("partial", finishReason="MAX_TOKENS"))
    ))
    self.assertEqual(events[0], {"type": "text", "text": "partial"})
    self.assertEqual(events[1]["type"], "error")
    self.assertIn("MAX_TOKENS", events[1]["message"])

    events = list(chat_lib.parse_stream(
        sse({"promptFeedback": {"blockReason": "SAFETY"}}, text_chunk("never"))
    ))
    self.assertEqual(len(events), 1)
    self.assertIn("SAFETY", events[0]["message"])

    events = list(chat_lib.parse_stream(
        sse({"error": {"code": 500, "message": "boom"}})
    ))
    self.assertEqual(events, [{"type": "error", "message": "boom"}])


class FakeGemini:
  """A local HTTP server that plays the part of the Gemini API."""

  def __init__(self, test):
    self.requests = []
    self.status = 200
    self.chunks = [text_chunk("Hello "), text_chunk("world", finishReason="STOP")]
    fake = self

    class Handler(http.server.BaseHTTPRequestHandler):

      def log_message(self, *args):
        pass

      def do_POST(self):
        length = int(self.headers["Content-Length"])
        fake.requests.append({
            "path": self.path,
            "key": self.headers.get("x-goog-api-key"),
            "body": json.loads(self.rfile.read(length)),
        })
        if fake.status != 200:
          body = json.dumps({"error": {"message": "API key not valid"}}).encode()
          self.send_response(fake.status)
          self.send_header("Content-Type", "application/json")
          self.send_header("Content-Length", str(len(body)))
          self.end_headers()
          self.wfile.write(body)
          return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in sse(*fake.chunks):
          self.wfile.write(line + b"\r\n")
          self.wfile.flush()

    self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
    thread.start()
    self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1beta"

    def stop():
      self.httpd.shutdown()
      self.httpd.server_close()
      thread.join()

    test.addCleanup(stop)


class ChatApiTest(unittest.TestCase):

  token = None

  def setUp(self):
    self.store = store_lib.Store(":memory:")
    self.addCleanup(self.store.close)
    self.store.put("guides/deploy.md", "# Deploy\n\nRoll out the canary first.", ["ops"])
    self.store.put("notes/misc.md", "# Misc\n\nUnrelated note.")
    self.gemini = FakeGemini(self)
    self.chat = chat_lib.Chat("test-key", model="test-model", base_url=self.gemini.url)
    self.port = self.serve(self.chat)

  def serve(self, chat):
    httpd = server_lib.Server(
        ("127.0.0.1", 0), self.store, token=self.token, chat=chat
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def stop():
      httpd.shutdown()
      httpd.server_close()
      thread.join()

    self.addCleanup(stop)
    return httpd.server_port

  def call(self, method, body=None, port=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port or self.port)
    self.addCleanup(conn.close)
    headers = dict(headers or {})
    if self.token and "Authorization" not in headers:
      headers["Authorization"] = f"Bearer {self.token}"
    if body is not None:
      headers["Content-Type"] = "application/json"
      body = json.dumps(body)
    conn.request(method, "/api/chat", body=body, headers=headers)
    response = conn.getresponse()
    data = response.read().decode()
    if response.getheader("Content-Type", "").startswith("application/x-ndjson"):
      return response.status, [json.loads(line) for line in data.splitlines()]
    return response.status, json.loads(data)

  def ask(self, text="How do I deploy?", **extra):
    return self.call("POST", {"messages": [{"role": "user", "text": text}], **extra})

  def test_status(self):
    self.assertEqual(
        self.call("GET"),
        (200, {"enabled": True, "model": "test-model",
               "token_required": bool(self.token)}),
    )
    port = self.serve(None)
    status, body = self.call("GET", port=port)
    self.assertEqual((status, body["enabled"], body["model"]), (200, False, None))
    status, body = self.call(
        "POST", {"messages": [{"role": "user", "text": "hi"}]}, port=port
    )
    self.assertEqual(status, 503)
    self.assertIn("GEMINI_API_KEY", body["error"])

  def test_answer_is_streamed_with_all_documents_in_context(self):
    status, events = self.ask()
    self.assertEqual(status, 200)
    self.assertEqual(
        events[0],
        {"type": "context", "model": "test-model", "documents": 2,
         "characters": len("# Deploy\n\nRoll out the canary first.")
         + len("# Misc\n\nUnrelated note."), "omitted": []},
    )
    self.assertEqual(
        [e["text"] for e in events if e["type"] == "text"], ["Hello ", "world"]
    )
    self.assertEqual(events[-1], {"type": "done"})

    sent = self.gemini.requests[0]
    self.assertEqual(
        sent["path"], "/v1beta/models/test-model:streamGenerateContent?alt=sse"
    )
    self.assertEqual(sent["key"], "test-key")
    system = sent["body"]["systemInstruction"]["parts"][0]["text"]
    self.assertIn("Roll out the canary first.", system)
    self.assertIn("Unrelated note.", system)
    self.assertEqual(
        sent["body"]["contents"],
        [{"role": "user", "parts": [{"text": "How do I deploy?"}]}],
    )

  def test_paths_limit_the_context(self):
    _, events = self.ask(paths=["guides/deploy.md", "gone/missing.md"])
    self.assertEqual(events[0]["documents"], 1)
    system = self.gemini.requests[0]["body"]["systemInstruction"]["parts"][0]["text"]
    self.assertIn("Roll out the canary first.", system)
    self.assertNotIn("Unrelated note.", system)

    _, events = self.ask(paths=[])
    self.assertEqual(events[0]["documents"], 0)

  def test_documents_over_the_budget_are_reported(self):
    self.chat.max_context_chars = 30
    _, events = self.ask()
    self.assertEqual(events[0]["documents"], 1)
    self.assertEqual(events[0]["omitted"], ["guides/deploy.md"])

  def test_conversation_history_is_forwarded(self):
    self.call("POST", {"messages": [
        {"role": "user", "text": "q1"},
        {"role": "model", "text": "a1"},
        {"role": "user", "text": "q2"},
    ]})
    self.assertEqual(
        [(c["role"], c["parts"][0]["text"])
         for c in self.gemini.requests[0]["body"]["contents"]],
        [("user", "q1"), ("model", "a1"), ("user", "q2")],
    )

  def test_invalid_requests_are_rejected_before_calling_the_model(self):
    for payload in [
        {},
        {"messages": []},
        {"messages": [{"role": "model", "text": "x"}]},
        {"messages": [{"role": "user", "text": "x"}], "paths": "a.md"},
        {"messages": [{"role": "user", "text": "x"}], "paths": ["../a.md"]},
        {"messages": [{"role": "user", "text": "x"}], "paths": [1]},
    ]:
      with self.subTest(payload=payload):
        status, body = self.call("POST", payload)
        self.assertEqual(status, 400)
        self.assertIn("error", body)
    self.assertEqual(self.gemini.requests, [])

  def test_model_api_errors_become_error_events(self):
    self.gemini.status = 400
    status, events = self.ask()
    self.assertEqual(status, 200)
    self.assertEqual(events[0]["type"], "context")
    self.assertEqual(events[1]["type"], "error")
    self.assertIn("HTTP 400", events[1]["message"])
    self.assertIn("API key not valid", events[1]["message"])
    self.assertNotIn("test-key", json.dumps(events))

  def test_unreachable_model_api_becomes_an_error_event(self):
    self.chat.base_url = "http://127.0.0.1:9/v1beta"
    _, events = self.ask()
    self.assertEqual(events[1]["type"], "error")
    self.assertIn("cannot reach", events[1]["message"])

  def test_early_stop_is_reported_after_the_partial_text(self):
    self.gemini.chunks = [text_chunk("partial", finishReason="SAFETY")]
    _, events = self.ask()
    self.assertEqual(
        [e["type"] for e in events], ["context", "text", "error", "done"]
    )


class ChatApiTokenTest(ChatApiTest):

  token = "s3cret"

  def test_chat_needs_the_token(self):
    for headers in [{"Authorization": "Bearer wrong"}, {"Authorization": ""}]:
      status, body = self.call(
          "POST", {"messages": [{"role": "user", "text": "hi"}]}, headers=headers
      )
      self.assertEqual(status, 401)
    self.assertEqual(self.gemini.requests, [])
    # The status endpoint stays open and says a token is needed.
    status, body = self.call("GET", headers={"Authorization": ""})
    self.assertEqual((status, body["token_required"]), (200, True))


if __name__ == "__main__":
  unittest.main()
