"""Two-way sync between this server and another mddb server.

This side always starts the conversation: it pulls the other server's changes,
then pushes its own. Each path ends up in whichever state was written last, on
both servers (see ``Store.apply_changes``).

Protocol, served by every mddb server:

- ``GET  /api/sync/changes?since=N&exclude_origin=ID`` returns a page of
  changes after position ``N`` of that server's change feed.
- ``POST /api/sync/changes`` with ``{"instance_id": ID, "changes": [...]}``
  applies changes.

Feed positions are remembered per remote URL, so a later sync only transfers
what changed since. If the remote database was replaced (its instance id
changed), everything is exchanged again from the start.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request

from mddb import store as store_lib

log = logging.getLogger("mddb.sync")

_PUSH_LIMIT = 100
_PUSH_BYTES = 1024 * 1024


class SyncError(Exception):
  """Syncing with the remote server failed."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
  """Refuses redirects, so the token is never sent to another host."""

  def redirect_request(self, req, fp, code, msg, headers, newurl):
    return None


def normalize_remote_url(url: str) -> str:
  """Validates the base URL of a remote server and strips trailing slashes."""
  url = url.strip().rstrip("/")
  parts = urllib.parse.urlsplit(url)
  if parts.scheme not in ("http", "https") or not parts.netloc:
    raise ValueError(
        "remote URL must look like https://host[:port], got " + repr(url)
    )
  if parts.query or parts.fragment or parts.username or parts.password:
    raise ValueError(
        "remote URL must not contain credentials, a query or a fragment"
    )
  return url


class Syncer:
  """Syncs one local store with one remote server."""

  def __init__(
      self,
      store: store_lib.Store,
      remote_url: str,
      token: str | None = None,
      timeout: float = 30,
  ):
    self.store = store
    self.remote_url = normalize_remote_url(remote_url)
    self._token = token or None
    self._timeout = timeout
    self._opener = urllib.request.build_opener(_NoRedirects)
    self._run_lock = threading.Lock()
    self._status_lock = threading.Lock()
    self._status = {
        "remote": self.remote_url,
        "running": False,
        "last_started": None,
        "last_finished": None,
        "last_result": None,
        "last_error": None,
    }

  def status(self) -> dict:
    with self._status_lock:
      return dict(self._status)

  def run(self) -> dict:
    """Runs one full sync and returns counts of what was transferred."""
    with self._run_lock:
      self._set_status(running=True, last_started=_now())
      try:
        result = self._sync()
      except SyncError as e:
        self._set_status(
            running=False, last_finished=_now(), last_error=str(e)
        )
        raise
      except Exception as e:  # pylint: disable=broad-except
        self._set_status(
            running=False, last_finished=_now(), last_error=repr(e)
        )
        raise SyncError(f"unexpected error: {e!r}") from e
      self._set_status(
          running=False,
          last_finished=_now(),
          last_result=result,
          last_error=None,
      )
      return result

  def _set_status(self, **fields) -> None:
    with self._status_lock:
      self._status.update(fields)

  def _sync(self) -> dict:
    result = {
        "pulled": 0,
        "pull_skipped": 0,
        "pushed": 0,
        "push_skipped": 0,
        "rejected": 0,
    }
    peer = self.store.get_peer(self.remote_url)
    pull_cursor = peer["pull_cursor"]
    push_cursor = peer["push_cursor"]
    remote_id = peer["instance_id"]

    # Pull: apply the remote's changes here.
    while True:
      query = urllib.parse.urlencode({
          "since": pull_cursor,
          "exclude_origin": self.store.instance_id,
      })
      page = self._request("GET", f"/api/sync/changes?{query}")
      page_id = page.get("instance_id")
      changes = page.get("changes")
      next_since = page.get("next_since")
      if (
          not isinstance(page_id, str)
          or not page_id
          or not isinstance(changes, list)
          or not isinstance(next_since, int)
      ):
        raise SyncError("remote sent a malformed change feed")
      if page_id == self.store.instance_id:
        raise SyncError(
            "the remote is this same database; refusing to sync with itself"
        )
      if page_id != remote_id:
        if remote_id is not None:
          log.warning(
              "remote database at %s was replaced; syncing from the start",
              self.remote_url,
          )
        remote_id = page_id
        if pull_cursor or push_cursor:
          # The remembered positions belong to another database.
          pull_cursor = push_cursor = 0
          self.store.save_peer(self.remote_url, remote_id, 0, 0)
          continue
      applied = self.store.apply_changes(changes, origin=remote_id)
      result["pulled"] += applied["applied"]
      result["pull_skipped"] += applied["skipped"]
      result["rejected"] += len(applied["rejected"])
      for rejected in applied["rejected"]:
        log.warning("ignored invalid change from remote: %s", rejected)
      if next_since < pull_cursor or (page.get("more") and not changes):
        raise SyncError("remote change feed did not advance")
      pull_cursor = next_since
      self.store.save_peer(
          self.remote_url, remote_id, pull_cursor, push_cursor
      )
      if not page.get("more"):
        break

    # Push: send local changes that did not come from the remote.
    while True:
      page = self.store.changes(
          since=push_cursor,
          limit=_PUSH_LIMIT,
          exclude_origin=remote_id,
          max_bytes=_PUSH_BYTES,
      )
      if page["changes"]:
        reply = self._request(
            "POST",
            "/api/sync/changes",
            {
                "instance_id": self.store.instance_id,
                "changes": page["changes"],
            },
        )
        try:
          result["pushed"] += int(reply["applied"])
          result["push_skipped"] += int(reply["skipped"])
          rejected = list(reply["rejected"])
        except (KeyError, TypeError, ValueError):
          raise SyncError("remote sent a malformed reply to a push") from None
        result["rejected"] += len(rejected)
        for entry in rejected:
          log.warning("remote rejected a change: %s", entry)
      push_cursor = page["next_since"]
      self.store.save_peer(
          self.remote_url, remote_id, pull_cursor, push_cursor
      )
      if not page["more"]:
        break
    return result

  def _request(self, method: str, path: str, payload=None) -> dict:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
      data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
      headers["Content-Type"] = "application/json"
      if self._token:
        headers["Authorization"] = f"Bearer {self._token}"
    request = urllib.request.Request(
        self.remote_url + path, data=data, headers=headers, method=method
    )
    try:
      with self._opener.open(request, timeout=self._timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
      detail = ""
      try:
        detail = json.loads(e.read().decode("utf-8")).get("error", "")
      except Exception:  # pylint: disable=broad-except
        pass
      if e.code in (301, 302, 303, 307, 308):
        detail = "redirects are not followed; use the final URL"
      elif e.code == 401:
        detail = detail or "the remote requires a token (--sync-token)"
      raise SyncError(
          f"{method} {path.split('?')[0]} failed with HTTP {e.code}"
          + (f": {detail}" if detail else "")
      ) from None
    except (urllib.error.URLError, OSError) as e:
      reason = getattr(e, "reason", e)
      raise SyncError(f"cannot reach {self.remote_url}: {reason}") from None
    except ValueError:
      raise SyncError(
          f"{self.remote_url} did not answer with JSON; is it an mddb server?"
      ) from None
    if not isinstance(body, dict):
      raise SyncError("remote sent an unexpected response")
    return body


class SyncWorker(threading.Thread):
  """Runs a Syncer in the background, every ``interval`` seconds."""

  def __init__(self, syncer: Syncer, interval: float):
    super().__init__(name="mddb-sync", daemon=True)
    self.syncer = syncer
    self.interval = interval
    self._stop_event = threading.Event()

  def run(self) -> None:
    while not self._stop_event.is_set():
      try:
        result = self.syncer.run()
        if any(result.values()):
          log.info("synced with %s: %s", self.syncer.remote_url, result)
      except SyncError as e:
        log.warning("sync with %s failed: %s", self.syncer.remote_url, e)
      self._stop_event.wait(self.interval)

  def stop(self) -> None:
    self._stop_event.set()


def _now() -> str:
  now = datetime.datetime.now(datetime.timezone.utc)
  return now.isoformat(timespec="seconds").replace("+00:00", "Z")
