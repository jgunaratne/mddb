"""Tests for the storage layer and the HTTP API."""

import http.client
import json
import threading
import unittest

from mddb import server as server_lib
from mddb import store as store_lib


class StoreTest(unittest.TestCase):

  def setUp(self):
    self.store = store_lib.Store(":memory:")
    self.addCleanup(self.store.close)

  def test_put_and_get_round_trip(self):
    doc, created = self.store.put(
        "skills/py/testing.md", "# Testing\n\nUse unittest.", ["Python", " qa "]
    )
    self.assertTrue(created)
    self.assertEqual(doc.title, "Testing")
    self.assertEqual(doc.tags, ["python", "qa"])
    self.assertEqual(doc.size_bytes, len("# Testing\n\nUse unittest."))
    fetched = self.store.get("/skills/py/testing.md")
    self.assertEqual(fetched.content, "# Testing\n\nUse unittest.")
    self.assertEqual(fetched.created_at, doc.created_at)

  def test_put_replaces_and_keeps_tags_when_not_given(self):
    self.store.put("a.md", "one", ["x"])
    doc, created = self.store.put("a.md", "two")
    self.assertFalse(created)
    self.assertEqual(doc.content, "two")
    self.assertEqual(doc.tags, ["x"])
    doc, _ = self.store.put("a.md", "three", [])
    self.assertEqual(doc.tags, [])
    self.assertEqual(self.store.list()[1], 1)

  def test_title_falls_back_to_file_name_and_skips_front_matter(self):
    doc, _ = self.store.put("notes/plain.md", "no heading here")
    self.assertEqual(doc.title, "plain")
    doc, _ = self.store.put(
        "s.md", "---\nname: x\n# not a title\n---\n```\n# code\n```\n# Real ##\n"
    )
    self.assertEqual(doc.title, "Real")

  def test_invalid_paths_are_rejected(self):
    for path in ["", "a.txt", "../a.md", "a//b.md", "a/./b.md", ".md", "a\\b.md",
                 "a/ b.md", "a\nb.md", "x" * 600 + ".md", "dir/.md"]:
      with self.subTest(path=path):
        with self.assertRaises(store_lib.InvalidPathError):
          self.store.put(path, "x")

  def test_invalid_tags_are_rejected(self):
    for tags in ["abc", ["a,b"], [1], ["x" * 100]]:
      with self.subTest(tags=tags):
        with self.assertRaises(store_lib.InvalidTagError):
          self.store.put("a.md", "x", tags)

  def test_get_and_delete_missing_raise_not_found(self):
    with self.assertRaises(store_lib.NotFoundError):
      self.store.get("missing.md")
    with self.assertRaises(store_lib.NotFoundError):
      self.store.delete("missing.md")

  def test_delete_removes_document_tags_and_search_entry(self):
    self.store.put("a.md", "zebra", ["x"])
    self.store.delete("a.md")
    self.assertEqual(self.store.list(), ([], 0))
    self.assertEqual(self.store.list(query="zebra"), ([], 0))
    self.assertEqual(self.store.tags(), [])

  def test_list_is_sorted_and_omits_content(self):
    self.store.put("b.md", "x")
    self.store.put("a/z.md", "x")
    docs, total = self.store.list()
    self.assertEqual([d.path for d in docs], ["a/z.md", "b.md"])
    self.assertEqual(total, 2)
    self.assertIsNone(docs[0].content)
    self.assertNotIn("content", docs[0].to_dict())

  def test_list_filters_by_all_tags_and_prefix(self):
    self.store.put("skills/a.md", "x", ["python", "testing"])
    self.store.put("skills/b.md", "x", ["python"])
    self.store.put("notes/c.md", "x", ["python", "testing"])
    paths = lambda **kw: [d.path for d in self.store.list(**kw)[0]]
    self.assertEqual(paths(tags=["python"]),
                     ["notes/c.md", "skills/a.md", "skills/b.md"])
    self.assertEqual(paths(tags=["Python", "testing"]),
                     ["notes/c.md", "skills/a.md"])
    self.assertEqual(paths(tags=["testing"], prefix="skills/"), ["skills/a.md"])
    self.assertEqual(paths(tags=["nope"]), [])
    self.assertEqual(paths(prefix="skills_%"), [])

  def test_limit_and_offset_page_through_results(self):
    for name in "abcde":
      self.store.put(f"{name}.md", "x")
    docs, total = self.store.list(limit=2, offset=3)
    self.assertEqual([d.path for d in docs], ["d.md", "e.md"])
    self.assertEqual(total, 5)

  def test_search_matches_content_title_path_and_tags(self):
    self.store.put("guides/deploy.md", "# Shipping\n\nRoll out the canary first.")
    self.store.put("misc/other.md", "nothing relevant", ["kubernetes"])
    search = lambda q: [d.path for d in self.store.list(query=q)[0]]
    self.assertEqual(search("canary"), ["guides/deploy.md"])
    self.assertEqual(search("CANAR"), ["guides/deploy.md"])  # prefix, any case
    self.assertEqual(search("shipping"), ["guides/deploy.md"])
    self.assertEqual(search("deploy"), ["guides/deploy.md"])
    self.assertEqual(search("kubernetes"), ["misc/other.md"])
    self.assertEqual(search("canary kubernetes"), [])  # all words must match
    self.assertEqual(search("zzz"), [])

  def test_search_ranks_title_matches_first_and_combines_with_tags(self):
    self.store.put("a.md", "# Other\n\nmentions rollback once", ["ops"])
    self.store.put("b.md", "# Rollback\n\nprocedure", ["ops"])
    self.store.put("c.md", "# Rollback\n\nprocedure", ["dev"])
    docs, total = self.store.list(query="rollback", tags=["ops"])
    self.assertEqual([d.path for d in docs], ["b.md", "a.md"])
    self.assertEqual(total, 2)

  def test_search_snippet_is_escaped_and_highlighted(self):
    self.store.put("a.md", "before <script>alert(1)</script> needle after")
    docs, _ = self.store.list(query="needle")
    self.assertIn("<mark>needle</mark>", docs[0].snippet_html)
    self.assertIn("&lt;script&gt;", docs[0].snippet_html)
    self.assertNotIn("<script>", docs[0].snippet_html)

  def test_search_tolerates_query_syntax_characters(self):
    self.store.put("a.md", "hello world")
    for query in ['"', "hello AND", "a* OR (", "-hello", "col:hello", "  "]:
      with self.subTest(query=query):
        self.store.list(query=query)  # Must not raise.

  def test_search_reflects_updates(self):
    self.store.put("a.md", "apple", ["fruit"])
    self.store.put("a.md", "banana")
    self.assertEqual(self.store.list(query="apple")[1], 0)
    self.assertEqual(self.store.list(query="banana")[1], 1)
    self.assertEqual(self.store.list(query="fruit")[1], 1)

  def test_tags_are_counted(self):
    self.store.put("a.md", "x", ["one", "two"])
    self.store.put("b.md", "x", ["two"])
    self.assertEqual(
        self.store.tags(),
        [{"tag": "two", "count": 2}, {"tag": "one", "count": 1}],
    )

  def test_data_persists_across_reopen(self):
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
      path = f"{tmp}/nested/db.sqlite3"
      first = store_lib.Store(path)
      first.put("a.md", "persisted words", ["keep"])
      first.close()
      second = store_lib.Store(path)
      self.addCleanup(second.close)
      self.assertEqual(second.get("a.md").tags, ["keep"])
      self.assertEqual(second.list(query="persisted")[1], 1)


class ApiTest(unittest.TestCase):

  token = None

  def setUp(self):
    self.store = store_lib.Store(":memory:")
    self.httpd = server_lib.Server(
        ("127.0.0.1", 0), self.store, token=self.token, max_body_bytes=4096
    )
    thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
    thread.start()

    def stop():
      self.httpd.shutdown()
      self.httpd.server_close()
      thread.join()
      self.store.close()

    self.addCleanup(stop)

  def request(self, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
    self.addCleanup(conn.close)
    headers = dict(headers or {})
    if isinstance(body, (dict, list)):
      body = json.dumps(body)
      headers.setdefault("Content-Type", "application/json")
    if isinstance(body, str):
      body = body.encode("utf-8")
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type", "")
    is_json = data and content_type.startswith("application/json")
    parsed = json.loads(data) if is_json else data
    return response, parsed

  def test_put_raw_markdown_then_get_json_and_raw(self):
    response, doc = self.request(
        "PUT",
        "/api/documents/skills/hello%20world.md?tags=a,b&tag=c",
        "# Hello\n\nbody ✓",
        {"Content-Type": "text/markdown; charset=utf-8"},
    )
    self.assertEqual(response.status, 201)
    self.assertEqual(doc["path"], "skills/hello world.md")
    self.assertEqual(doc["tags"], ["a", "b", "c"])
    self.assertEqual(doc["title"], "Hello")

    response, doc = self.request("GET", "/api/documents/skills/hello%20world.md")
    self.assertEqual(response.status, 200)
    self.assertEqual(doc["content"], "# Hello\n\nbody ✓")

    for path, headers in [
        ("/api/documents/skills/hello%20world.md?raw=1", {}),
        ("/api/documents/skills/hello%20world.md", {"Accept": "text/markdown"}),
    ]:
      response, raw = self.request("GET", path, headers=headers)
      self.assertEqual(response.status, 200)
      self.assertTrue(response.getheader("Content-Type").startswith("text/markdown"))
      self.assertIn("sandbox", response.getheader("Content-Security-Policy"))
      self.assertEqual(raw.decode("utf-8"), "# Hello\n\nbody ✓")

  def test_put_json_updates_and_reports_status(self):
    response, _ = self.request(
        "PUT", "/api/documents/a.md", {"content": "one", "tags": ["t"]}
    )
    self.assertEqual(response.status, 201)
    response, doc = self.request("PUT", "/api/documents/a.md", {"content": "two"})
    self.assertEqual(response.status, 200)
    self.assertEqual(doc["content"], "two")
    self.assertEqual(doc["tags"], ["t"])

  def test_post_creates_document(self):
    response, doc = self.request(
        "POST", "/api/documents", {"path": "n/x.md", "content": "hi", "tags": ["t"]}
    )
    self.assertEqual(response.status, 201)
    self.assertEqual(doc["path"], "n/x.md")
    response, body = self.request("POST", "/api/documents", {"content": "hi"})
    self.assertEqual(response.status, 400)
    self.assertIn("path", body["error"])

  def test_list_search_and_tags(self):
    self.request("PUT", "/api/documents/a.md", {"content": "alpha", "tags": ["x", "y"]})
    self.request("PUT", "/api/documents/d/b.md", {"content": "beta", "tags": ["x"]})

    response, body = self.request("GET", "/api/documents")
    self.assertEqual(response.status, 200)
    self.assertEqual(body["total"], 2)
    self.assertEqual([d["path"] for d in body["documents"]], ["a.md", "d/b.md"])
    self.assertNotIn("content", body["documents"][0])

    _, body = self.request("GET", "/api/documents?q=beta")
    self.assertEqual([d["path"] for d in body["documents"]], ["d/b.md"])
    self.assertIn("<mark>beta</mark>", body["documents"][0]["snippet_html"])

    _, body = self.request("GET", "/api/documents?tag=x&tag=y")
    self.assertEqual([d["path"] for d in body["documents"]], ["a.md"])
    _, body = self.request("GET", "/api/documents?tags=x,y")
    self.assertEqual([d["path"] for d in body["documents"]], ["a.md"])
    _, body = self.request("GET", "/api/documents?prefix=d/&limit=1")
    self.assertEqual([d["path"] for d in body["documents"]], ["d/b.md"])

    _, body = self.request("GET", "/api/tags")
    self.assertEqual(
        body["tags"], [{"tag": "x", "count": 2}, {"tag": "y", "count": 1}]
    )

  def test_delete(self):
    self.request("PUT", "/api/documents/a.md", {"content": "x"})
    response, _ = self.request("DELETE", "/api/documents/a.md")
    self.assertEqual(response.status, 204)
    response, _ = self.request("GET", "/api/documents/a.md")
    self.assertEqual(response.status, 404)
    response, _ = self.request("DELETE", "/api/documents/a.md")
    self.assertEqual(response.status, 404)

  def test_errors(self):
    cases = [
        ("GET", "/api/documents/missing.md", None, {}, 404),
        ("GET", "/api/nope", None, {}, 404),
        ("GET", "/api/documents?limit=abc", None, {}, 400),
        ("PUT", "/api/documents/a.txt", {"content": "x"}, {}, 400),
        ("PUT", "/api/documents/%2e%2e/a.md", {"content": "x"}, {}, 400),
        ("PUT", "/api/documents/a.md", {"tags": ["x"]}, {}, 400),
        ("PUT", "/api/documents/a.md", {"content": "x", "tags": "x"}, {}, 400),
        ("PUT", "/api/documents/a.md", "{nope", {"Content-Type": "application/json"}, 400),
        ("PUT", "/api/documents/a.md", "[1]", {"Content-Type": "application/json"}, 400),
        ("PUT", "/api/documents/a.md", b"\xff\xfe", {"Content-Type": "text/markdown"}, 400),
        ("PUT", "/api/documents/a.md", "x", {"Content-Type": "text/plain"}, 415),
        ("POST", "/api/documents", "path=a.md", {"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ("PUT", "/api/documents/a.md", "x" * 5000, {"Content-Type": "text/markdown"}, 413),
        ("DELETE", "/api/documents", None, {}, 405),
        ("POST", "/api/tags", {}, {}, 405),
        ("POST", "/", {}, {}, 405),
    ]
    for method, path, body, headers, status in cases:
      with self.subTest(method=method, path=path, body=body):
        response, payload = self.request(method, path, body, headers)
        self.assertEqual(response.status, status)
        self.assertIn("error", payload)
    self.assertEqual(self.store.list()[1], 0)

  def test_keep_alive_survives_rejected_and_accepted_bodies(self):
    conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port)
    self.addCleanup(conn.close)
    for content_type, status in [("text/plain", 415), ("text/markdown", 201)]:
      headers = {"Content-Type": content_type}
      if self.token:
        headers["Authorization"] = f"Bearer {self.token}"
      conn.request("PUT", "/api/documents/k.md", body=b"hello", headers=headers)
      response = conn.getresponse()
      response.read()
      self.assertEqual(response.status, status)
    conn.request("GET", "/api/health")
    response = conn.getresponse()
    self.assertEqual(json.loads(response.read()), {"status": "ok"})

  def test_static_files_and_traversal(self):
    response, body = self.request("GET", "/")
    self.assertEqual(response.status, 200)
    self.assertTrue(response.getheader("Content-Type").startswith("text/html"))
    self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
    self.assertIn(b"<title>mddb</title>", body)
    for path in ["/app.js", "/styles.css", "/vendor/marked.umd.js", "/vendor/purify.min.js"]:
      with self.subTest(path=path):
        self.assertEqual(self.request("GET", path)[0].status, 200)
    for path in ["/../mddb/store.py", "/%2e%2e/mddb/store.py", "/..%2fREADME.md",
                 "/vendor/../../README.md", "/missing.js", "/vendor"]:
      with self.subTest(path=path):
        self.assertEqual(self.request("GET", path)[0].status, 404)

  def test_head_has_no_body(self):
    response, body = self.request("HEAD", "/api/health")
    self.assertEqual(response.status, 200)
    self.assertEqual(body, b"")


class ApiTokenTest(ApiTest):

  token = "s3cret"

  def request(self, method, path, body=None, headers=None, authorized=True):
    headers = dict(headers or {})
    if authorized:
      headers.setdefault("Authorization", f"Bearer {self.token}")
    return super().request(method, path, body, headers)

  def test_writes_need_the_token_but_reads_do_not(self):
    self.request("PUT", "/api/documents/a.md", {"content": "x"})
    for headers in [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "s3cret"}]:
      for method, path, body in [
          ("PUT", "/api/documents/a.md", {"content": "changed"}),
          ("POST", "/api/documents", {"path": "b.md", "content": "x"}),
          ("DELETE", "/api/documents/a.md", None),
      ]:
        with self.subTest(method=method, headers=headers):
          response, _ = self.request(method, path, body, headers, authorized=False)
          self.assertEqual(response.status, 401)
    response, doc = self.request("GET", "/api/documents/a.md", authorized=False)
    self.assertEqual(response.status, 200)
    self.assertEqual(doc["content"], "x")
    self.assertEqual(self.request("GET", "/api/documents", authorized=False)[1]["total"], 1)


if __name__ == "__main__":
  unittest.main()
