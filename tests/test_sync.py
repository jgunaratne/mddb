"""Tests for two-way sync between two servers."""

import json
import threading
import time
import unittest
import urllib.request

from mddb import server as server_lib
from mddb import store as store_lib
from mddb import sync as sync_lib


def later():
  """Waits long enough for the next write to get a later timestamp."""
  time.sleep(0.005)


def snapshot(store):
  """Everything observable about a store: documents plus deletions."""
  documents = {}
  for listed in store.list(limit=store_lib.MAX_LIMIT)[0]:
    doc = store.get(listed.path)
    documents[doc.path] = (doc.content, tuple(doc.tags), doc.updated_at)
  deleted = sorted(
      change["path"]
      for change in store.changes(limit=store_lib.MAX_CHANGES_LIMIT)["changes"]
      if change["deleted"]
  )
  return documents, deleted


class SyncTestCase(unittest.TestCase):

  remote_token = None

  def setUp(self):
    self.local = store_lib.Store(":memory:")
    self.remote = store_lib.Store(":memory:")
    self.httpd, self.url = self.serve(self.remote, token=self.remote_token)
    self.syncer = sync_lib.Syncer(self.local, self.url, token=self.remote_token)
    self.addCleanup(self.local.close)
    self.addCleanup(self.remote.close)

  def serve(self, store, **kwargs):
    httpd = server_lib.Server(("127.0.0.1", 0), store, **kwargs)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    def stop():
      httpd.shutdown()
      httpd.server_close()
      thread.join()

    self.addCleanup(stop)
    return httpd, f"http://127.0.0.1:{httpd.server_port}"

  def assert_in_sync(self):
    self.assertEqual(snapshot(self.local), snapshot(self.remote))


class SyncTest(SyncTestCase):

  def test_documents_flow_both_ways_with_tags_and_timestamps(self):
    self.local.put("here/a.md", "# From here", ["x", "y"])
    self.remote.put("there/b.md", "# From there", ["z"])

    result = self.syncer.run()

    self.assertEqual(result["pulled"], 1)
    self.assertEqual(result["pushed"], 1)
    self.assertEqual(result["rejected"], 0)
    self.assert_in_sync()
    self.assertEqual(self.local.get("there/b.md").tags, ["z"])
    self.assertEqual(self.remote.get("here/a.md").tags, ["x", "y"])
    self.assertEqual(self.remote.get("here/a.md").title, "From here")
    # Synced documents are searchable on the receiving side.
    self.assertEqual(self.remote.list(query="here")[1], 1)
    self.assertEqual(self.local.list(query="there", tags=["z"])[1], 1)

  def test_second_sync_transfers_nothing(self):
    self.local.put("a.md", "one")
    self.remote.put("b.md", "two")
    self.syncer.run()
    for _ in range(2):
      result = self.syncer.run()
      self.assertEqual(
          result,
          {"pulled": 0, "pull_skipped": 0, "pushed": 0, "push_skipped": 0,
           "rejected": 0},
      )
    # Nothing is echoed back: the remote has no new changes for us either.
    peer = self.local.get_peer(self.url)
    feed = self.remote.changes(
        since=peer["pull_cursor"], exclude_origin=self.local.instance_id
    )
    self.assertEqual(feed["changes"], [])

  def test_updates_and_tag_changes_propagate(self):
    self.local.put("a.md", "v1", ["old"])
    self.syncer.run()
    later()
    self.remote.put("a.md", "v2", ["new"])
    self.syncer.run()
    self.assertEqual(self.local.get("a.md").content, "v2")
    self.assertEqual(self.local.get("a.md").tags, ["new"])
    self.local.put("a.md", "v3")
    self.syncer.run()
    self.assertEqual(self.remote.get("a.md").content, "v3")
    self.assertEqual(self.remote.get("a.md").tags, ["new"])
    self.assert_in_sync()

  def test_deletes_propagate_both_ways(self):
    self.local.put("a.md", "x")
    self.local.put("b.md", "x")
    self.syncer.run()
    self.local.delete("a.md")
    self.remote.delete("b.md")
    self.syncer.run()
    self.assertEqual(self.local.list()[1], 0)
    self.assertEqual(self.remote.list()[1], 0)
    self.assertEqual(self.remote.list(query="x")[1], 0)
    self.assert_in_sync()

  def test_recreating_a_deleted_document_propagates(self):
    self.local.put("a.md", "first")
    self.syncer.run()
    self.remote.delete("a.md")
    self.syncer.run()
    self.local.put("a.md", "second")
    self.syncer.run()
    self.assertEqual(self.remote.get("a.md").content, "second")
    self.assert_in_sync()

  def test_conflict_newest_write_wins_on_both_sides(self):
    self.local.put("a.md", "base")
    self.syncer.run()
    self.local.put("a.md", "older edit here")
    later()
    self.remote.put("a.md", "newer edit there")
    self.syncer.run()
    self.assertEqual(self.local.get("a.md").content, "newer edit there")
    self.assertEqual(self.remote.get("a.md").content, "newer edit there")
    self.assert_in_sync()

  def test_conflict_newer_edit_beats_older_delete_and_vice_versa(self):
    self.local.put("keep.md", "base")
    self.local.put("drop.md", "base")
    self.syncer.run()
    self.remote.delete("keep.md")
    self.local.put("drop.md", "edited before the delete")
    later()
    self.local.put("keep.md", "edited after the delete")
    self.remote.delete("drop.md")
    self.syncer.run()
    self.assertEqual(self.remote.get("keep.md").content, "edited after the delete")
    with self.assertRaises(store_lib.NotFoundError):
      self.local.get("drop.md")
    self.assert_in_sync()

  def test_equal_timestamps_converge_to_the_same_winner(self):
    when = "2026-01-01T00:00:00.000Z"
    self.local.apply_changes([
        {"path": "a.md", "deleted": False, "updated_at": when, "content": "aaa", "tags": []},
        {"path": "b.md", "deleted": False, "updated_at": when, "content": "doc", "tags": []},
    ])
    self.remote.apply_changes([
        {"path": "a.md", "deleted": False, "updated_at": when, "content": "zzz", "tags": []},
        {"path": "b.md", "deleted": True, "updated_at": when},
    ])
    self.syncer.run()
    self.assertEqual(
        self.local.get("a.md").content, self.remote.get("a.md").content
    )
    self.assertEqual(self.local.list()[1], 1)  # The deletion of b.md won.
    self.assert_in_sync()

  def test_local_write_beats_state_stamped_in_the_future(self):
    future = "2999-01-01T00:00:00.000Z"
    self.remote.apply_changes([
        {"path": "a.md", "deleted": False, "updated_at": future, "content": "from a fast clock", "tags": []},
    ])
    self.syncer.run()
    self.local.put("a.md", "local edit")
    self.assertGreater(self.local.get("a.md").updated_at, future)
    self.syncer.run()
    self.assertEqual(self.remote.get("a.md").content, "local edit")
    self.local.delete("a.md")
    self.syncer.run()
    self.assertEqual(self.remote.list()[1], 0)

  def test_many_and_large_documents_are_paged(self):
    big = "word " * 60_000  # ~300 kB each; pushes and pulls need several pages.
    for index in range(12):
      self.local.put(f"local/{index:02}.md", big + str(index))
      self.remote.put(f"remote/{index:02}.md", big + str(index))
    for index in range(450):
      self.remote.put(f"small/{index:03}.md", f"small {index}")
    result = self.syncer.run()
    self.assertEqual(result["pulled"], 462)
    self.assertEqual(result["pushed"], 12)
    self.assertEqual(self.local.list(limit=5000)[1], 474)
    self.assert_in_sync()

  def test_remote_database_replaced_triggers_full_resync(self):
    self.local.put("a.md", "mine")
    self.remote.put("b.md", "theirs")
    self.syncer.run()
    # A fresh database appears at the same URL.
    fresh = store_lib.Store(":memory:")
    self.addCleanup(fresh.close)
    fresh.put("c.md", "new remote")
    self.httpd.store = fresh
    with self.assertLogs("mddb.sync", level="WARNING"):
      result = self.syncer.run()
    self.assertEqual(result["pulled"], 1)
    self.assertEqual(result["pushed"], 2)
    self.assertEqual(snapshot(self.local), snapshot(fresh))

  def test_three_servers_in_a_chain_converge(self):
    # local <-> remote <-> third, with remote relaying between the other two.
    third = store_lib.Store(":memory:")
    self.addCleanup(third.close)
    _, third_url = self.serve(third)
    relay = sync_lib.Syncer(self.remote, third_url)
    self.local.put("from-local.md", "l")
    third.put("from-third.md", "t")
    for _ in range(2):
      self.syncer.run()
      relay.run()
    self.syncer.run()
    self.assertEqual(snapshot(self.local), snapshot(third))
    self.assertEqual(self.local.list()[1], 2)
    self.assertEqual(
        self.syncer.run(),
        {"pulled": 0, "pull_skipped": 0, "pushed": 0, "push_skipped": 0,
         "rejected": 0},
    )

  def test_invalid_changes_are_rejected_without_blocking_the_rest(self):
    result = self.local.apply_changes([
        {"path": "../evil.md", "deleted": False, "updated_at": "2026-01-01T00:00:00.000Z", "content": "x"},
        {"path": "a.md", "deleted": False, "updated_at": "yesterday", "content": "x"},
        {"path": "b.md", "deleted": False, "updated_at": "2026-01-01T00:00:00.000Z"},
        {"path": "c.md", "deleted": "yes", "updated_at": "2026-01-01T00:00:00.000Z"},
        {"path": "d.md", "deleted": False, "updated_at": "2026-13-45T00:00:00.000Z", "content": "x"},
        "nonsense",
        {"path": "ok.md", "deleted": False, "updated_at": "2026-01-01T00:00:00.000Z", "content": "fine", "tags": ["T"]},
    ])
    self.assertEqual(result["applied"], 1)
    self.assertEqual(len(result["rejected"]), 6)
    self.assertEqual([d.path for d in self.local.list()[0]], ["ok.md"])
    self.assertEqual(self.local.get("ok.md").tags, ["t"])

  def test_sync_errors_are_reported(self):
    with self.assertRaisesRegex(sync_lib.SyncError, "cannot reach"):
      sync_lib.Syncer(self.local, "http://127.0.0.1:9", timeout=2).run()
    with self.assertRaisesRegex(sync_lib.SyncError, "same database"):
      _, own_url = self.serve(self.local)
      sync_lib.Syncer(self.local, own_url).run()
    for bad in ["ftp://x", "example.com", "http://user:pw@host", "http://h/?q=1"]:
      with self.subTest(url=bad), self.assertRaises(ValueError):
        sync_lib.Syncer(self.local, bad)
    status = self.syncer.status()
    self.assertIsNone(status["last_result"])


class SyncApiTest(SyncTestCase):
  """The HTTP endpoints, including a server that syncs on request."""

  def request(self, url, method="GET", payload=None, token=None):
    headers = {}
    data = None
    if payload is not None:
      data = json.dumps(payload).encode()
      headers["Content-Type"] = "application/json"
    if token:
      headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
      with urllib.request.urlopen(request) as response:
        return response.status, json.load(response)
    except urllib.error.HTTPError as e:
      return e.code, json.load(e)

  def test_changes_feed_pages_and_excludes_origin(self):
    self.remote.put("a.md", "1")
    self.remote.put("b.md", "2")
    self.remote.delete("a.md")
    self.remote.apply_changes(
        [{"path": "c.md", "deleted": False, "updated_at": "2026-01-01T00:00:00.000Z", "content": "3"}],
        origin="peer-1",
    )
    status, page = self.request(f"{self.url}/api/sync/changes?limit=1")
    self.assertEqual(status, 200)
    self.assertEqual(page["instance_id"], self.remote.instance_id)
    self.assertTrue(page["more"])
    self.assertEqual([c["path"] for c in page["changes"]], ["b.md"])
    _, page = self.request(
        f"{self.url}/api/sync/changes?since={page['next_since']}"
    )
    self.assertFalse(page["more"])
    self.assertEqual(
        [(c["path"], c["deleted"]) for c in page["changes"]],
        [("a.md", True), ("c.md", False)],
    )
    self.assertNotIn("content", page["changes"][0])
    _, page = self.request(f"{self.url}/api/sync/changes?exclude_origin=peer-1")
    self.assertEqual([c["path"] for c in page["changes"]], ["b.md", "a.md"])
    _, page = self.request(
        f"{self.url}/api/sync/changes?since={page['next_since']}"
    )
    self.assertEqual(page["changes"], [])

  def test_server_syncs_on_request_and_reports_status(self):
    self.local.put("a.md", "local")
    self.remote.put("b.md", "remote")
    _, local_url = self.serve(self.local, syncer=self.syncer)

    status, body = self.request(f"{local_url}/api/sync")
    self.assertEqual(status, 200)
    self.assertTrue(body["configured"])
    self.assertEqual(body["remote"], self.url)
    self.assertIsNone(body["last_finished"])

    status, body = self.request(f"{local_url}/api/sync", "POST", {})
    self.assertEqual(status, 200)
    self.assertEqual((body["pulled"], body["pushed"]), (1, 1))
    self.assert_in_sync()

    _, body = self.request(f"{local_url}/api/sync")
    self.assertEqual(body["last_result"]["pulled"], 1)
    self.assertIsNone(body["last_error"])
    self.assertFalse(body["running"])

    # The remote has no syncer of its own.
    status, body = self.request(f"{self.url}/api/sync")
    self.assertEqual((status, body), (200, {"configured": False}))
    status, _ = self.request(f"{self.url}/api/sync", "POST", {})
    self.assertEqual(status, 409)

  def test_unreachable_remote_is_a_bad_gateway(self):
    broken = sync_lib.Syncer(self.local, "http://127.0.0.1:9", timeout=2)
    _, local_url = self.serve(self.local, syncer=broken)
    status, body = self.request(f"{local_url}/api/sync", "POST", {})
    self.assertEqual(status, 502)
    self.assertIn("cannot reach", body["error"])
    _, body = self.request(f"{local_url}/api/sync")
    self.assertIn("cannot reach", body["last_error"])

  def test_push_endpoint_validates_input(self):
    for payload in [{"changes": []}, {"instance_id": "x"}, {"instance_id": "", "changes": []}]:
      with self.subTest(payload=payload):
        status, _ = self.request(f"{self.url}/api/sync/changes", "POST", payload)
        self.assertEqual(status, 400)
    status, _ = self.request(f"{self.url}/api/sync/changes", "DELETE")
    self.assertEqual(status, 405)


class SyncWithTokenTest(SyncTestCase):

  remote_token = "s3cret"

  def test_sync_uses_the_remote_token(self):
    self.local.put("a.md", "local")
    self.remote.put("b.md", "remote")
    self.syncer.run()
    self.assert_in_sync()

  def test_push_without_the_token_fails_but_changes_nothing(self):
    self.local.put("a.md", "local")
    self.remote.put("b.md", "remote")
    anonymous = sync_lib.Syncer(self.local, self.url)
    with self.assertRaisesRegex(sync_lib.SyncError, "401"):
      anonymous.run()
    self.assertEqual(self.remote.list()[1], 1)
    self.assertIn("401", anonymous.status()["last_error"])
    # With the token, the interrupted sync completes.
    self.syncer.run()
    self.assert_in_sync()


class SyncWorkerTest(SyncTestCase):

  def test_worker_syncs_repeatedly_until_stopped(self):
    self.remote.put("a.md", "first")
    worker = sync_lib.SyncWorker(self.syncer, interval=0.05)
    worker.start()
    self.addCleanup(worker.join, 5)
    self.addCleanup(worker.stop)
    self.wait_for(lambda: self.local.list()[1] == 1)
    self.local.put("b.md", "second")
    self.wait_for(lambda: self.remote.list()[1] == 2)
    self.assert_in_sync()

  def wait_for(self, condition):
    deadline = threading.Event()
    for _ in range(200):
      if condition():
        return
      deadline.wait(0.05)
    self.fail("condition was not reached in time")


if __name__ == "__main__":
  unittest.main()
