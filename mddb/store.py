"""SQLite-backed storage for markdown documents.

Documents are addressed by a slash-separated path ending in ``.md`` (for
example ``skills/python/testing.md``), carry a set of tags, and are indexed for
full-text search with SQLite FTS5.

Every write also gets a position in a change feed, and deletions are remembered
as tombstones, so that two servers can exchange changes (see ``mddb.sync``).
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import html
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from pathlib import Path

MAX_PATH_LENGTH = 512
MAX_TAG_LENGTH = 64
MAX_TAGS = 64
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
DEFAULT_CHANGES_LIMIT = 200
MAX_CHANGES_LIMIT = 1000
DEFAULT_CHANGES_BYTES = 2 * 1024 * 1024

# Weights for bm25() in column order: path, title, content, tags.
_BM25_WEIGHTS = "4.0, 8.0, 1.0, 4.0"
_SNIPPET_START = "\x02"
_SNIPPET_END = "\x03"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  -- Position in this database's change feed, and the instance id of the peer
  -- a synced change came from (NULL for changes made here).
  seq INTEGER NOT NULL,
  origin TEXT
);
CREATE INDEX IF NOT EXISTS documents_by_seq ON documents (seq);
-- Deleted paths, kept so that deletions can be synced to other servers.
CREATE TABLE IF NOT EXISTS tombstones (
  path TEXT PRIMARY KEY,
  deleted_at TEXT NOT NULL,
  seq INTEGER NOT NULL,
  origin TEXT
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS tombstones_by_seq ON tombstones (seq);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sync_peers (
  url TEXT PRIMARY KEY,
  instance_id TEXT,
  pull_cursor INTEGER NOT NULL DEFAULT 0,
  push_cursor INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS document_tags (
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY (document_id, tag)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS document_tags_by_tag
  ON document_tags (tag, document_id);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
  path, title, content, tags,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""


class StoreError(Exception):
  """Base class for storage errors."""


class InvalidPathError(StoreError, ValueError):
  """The document path is not acceptable."""


class InvalidTagError(StoreError, ValueError):
  """A tag is not acceptable."""


class NotFoundError(StoreError, KeyError):
  """No document exists at the requested path."""

  def __str__(self) -> str:
    return str(self.args[0]) if self.args else "not found"


@dataclasses.dataclass
class Document:
  """A stored markdown document. ``content`` is omitted in listings."""

  path: str
  title: str
  tags: list[str]
  size_bytes: int
  created_at: str
  updated_at: str
  content: str | None = None
  snippet_html: str | None = None

  def to_dict(self) -> dict:
    result = {
        "path": self.path,
        "title": self.title,
        "tags": self.tags,
        "size_bytes": self.size_bytes,
        "created_at": self.created_at,
        "updated_at": self.updated_at,
    }
    if self.content is not None:
      result["content"] = self.content
    if self.snippet_html is not None:
      result["snippet_html"] = self.snippet_html
    return result


def normalize_path(path: str) -> str:
  """Validates a document path and returns its canonical form."""
  if not isinstance(path, str):
    raise InvalidPathError("path must be a string")
  path = path.strip().lstrip("/")
  if not path:
    raise InvalidPathError("path must not be empty")
  if len(path) > MAX_PATH_LENGTH:
    raise InvalidPathError(f"path is longer than {MAX_PATH_LENGTH} characters")
  if "\\" in path or any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
    raise InvalidPathError(
        "path must not contain backslashes or control characters"
    )
  if not path.lower().endswith(".md"):
    raise InvalidPathError("path must end with .md")
  for segment in path.split("/"):
    if not segment:
      raise InvalidPathError("path must not contain empty segments")
    if segment in (".", ".."):
      raise InvalidPathError("path must not contain '.' or '..' segments")
    if segment != segment.strip():
      raise InvalidPathError(
          "path segments must not start or end with whitespace"
      )
  if path.rsplit("/", 1)[-1].lower() == ".md":
    raise InvalidPathError("file name must not be empty")
  return path


def normalize_tags(tags: Iterable[str] | None) -> list[str]:
  """Lowercases, trims, de-duplicates and sorts tags."""
  if tags is None:
    return []
  if isinstance(tags, str):
    raise InvalidTagError("tags must be a list of strings")
  result = set()
  for tag in tags:
    if not isinstance(tag, str):
      raise InvalidTagError("tags must be strings")
    tag = " ".join(tag.split()).lower()
    if not tag:
      continue
    if len(tag) > MAX_TAG_LENGTH:
      raise InvalidTagError(f"tag is longer than {MAX_TAG_LENGTH} characters")
    if "," in tag or any(ord(ch) < 32 or ord(ch) == 127 for ch in tag):
      raise InvalidTagError(
          "tags must not contain commas or control characters"
      )
    result.add(tag)
  if len(result) > MAX_TAGS:
    raise InvalidTagError(f"a document can have at most {MAX_TAGS} tags")
  return sorted(result)


def derive_title(path: str, content: str) -> str:
  """Returns the first level-1 heading, or the file name without ``.md``."""
  lines = content.splitlines()
  start = 0
  if lines and lines[0].strip() == "---":
    for index in range(1, len(lines)):
      if lines[index].strip() in ("---", "..."):
        start = index + 1
        break
  in_fence = False
  for line in lines[start:]:
    stripped = line.strip()
    if stripped.startswith(("```", "~~~")):
      in_fence = not in_fence
      continue
    if not in_fence:
      match = re.match(r"#\s+(.+?)\s*#*\s*$", stripped)
      if match:
        return match.group(1)
  return path.rsplit("/", 1)[-1][:-3]


def build_fts_query(query: str | None) -> str | None:
  """Turns free text into an FTS5 query: every word must match as a prefix."""
  if not query:
    return None
  words = re.findall(r"\w+", query, flags=re.UNICODE)
  if not words:
    return None
  return " ".join(f'"{word}"*' for word in words)


_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
_TIME_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def _format_time(when: datetime.datetime) -> str:
  return when.strftime(_TIME_FORMAT)[:-3] + "Z"


def _now() -> str:
  return _format_time(datetime.datetime.now(datetime.timezone.utc))


def _parse_time(value) -> datetime.datetime:
  """Parses the fixed-width UTC timestamps used for versions."""
  if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
    raise StoreError("timestamps must look like 2026-01-31T12:00:00.000Z")
  try:
    return datetime.datetime.strptime(value[:-1], _TIME_FORMAT)
  except ValueError:
    raise StoreError(f"invalid timestamp {value!r}") from None


def _digest(content: str, tags: list[str]) -> str:
  data = content + "\0" + ",".join(tags)
  return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _snippet_to_html(snippet: str) -> str:
  escaped = html.escape(snippet, quote=False)
  return escaped.replace(_SNIPPET_START, "<mark>").replace(
      _SNIPPET_END, "</mark>"
  )


class Store:
  """Thread-safe document store on top of a single SQLite database file."""

  def __init__(self, db_path: str | Path):
    self._lock = threading.RLock()
    if str(db_path) != ":memory:":
      Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
      db_path = str(Path(db_path).expanduser())
    self._conn = sqlite3.connect(db_path, check_same_thread=False)
    self._conn.row_factory = sqlite3.Row
    with self._lock, self._conn:
      self._conn.execute("PRAGMA foreign_keys = ON")
      if db_path != ":memory:":
        self._conn.execute("PRAGMA journal_mode = WAL")
      self._conn.executescript(_SCHEMA)
      self._conn.execute(
          "INSERT OR IGNORE INTO meta (key, value) VALUES ('seq', '0')"
      )
      self._conn.execute(
          "INSERT OR IGNORE INTO meta (key, value) VALUES ('instance_id', ?)",
          (uuid.uuid4().hex,),
      )
      self.instance_id: str = self._conn.execute(
          "SELECT value FROM meta WHERE key = 'instance_id'"
      ).fetchone()[0]

  def close(self) -> None:
    with self._lock:
      self._conn.close()

  def put(
      self,
      path: str,
      content: str,
      tags: Iterable[str] | None = None,
  ) -> tuple[Document, bool]:
    """Creates or replaces a document.

    Args:
      path: Document path, e.g. ``notes/todo.md``.
      content: Markdown text.
      tags: New tags. ``None`` keeps the existing tags of a document that is
        being replaced.

    Returns:
      The stored document and whether it was newly created.
    """
    path = normalize_path(path)
    if not isinstance(content, str):
      raise StoreError("content must be a string")
    new_tags = None if tags is None else normalize_tags(tags)
    with self._lock, self._conn:
      when = self._local_time(path)
      created = self._write_document(path, content, new_tags, when, when, None)
      return self._get_locked(path), created

  def get(self, path: str) -> Document:
    """Returns the document at ``path`` including its content."""
    path = normalize_path(path)
    with self._lock:
      return self._get_locked(path)

  def delete(self, path: str) -> None:
    """Deletes the document at ``path``."""
    path = normalize_path(path)
    with self._lock, self._conn:
      row = self._conn.execute(
          "SELECT id FROM documents WHERE path = ?", (path,)
      ).fetchone()
      if row is None:
        raise NotFoundError(f"no document at {path!r}")
      self._write_tombstone(path, self._local_time(path), None)

  def list(
      self,
      query: str | None = None,
      tags: Iterable[str] | None = None,
      prefix: str | None = None,
      limit: int = DEFAULT_LIMIT,
      offset: int = 0,
  ) -> tuple[list[Document], int]:
    """Lists document metadata, optionally filtered.

    Args:
      query: Free-text search over path, title, content and tags. Every word
        must match (as a prefix). Results are ranked by relevance.
      tags: Only documents carrying all of these tags are returned.
      prefix: Only documents whose path starts with this string are returned.
      limit: Maximum number of documents to return.
      offset: Number of documents to skip.

    Returns:
      The matching documents (without content) and the total match count.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    wanted_tags = normalize_tags(tags)
    fts_query = build_fts_query(query)

    where = []
    params: list = []
    if fts_query:
      source = "documents_fts JOIN documents d ON d.id = documents_fts.rowid"
      where.append("documents_fts MATCH ?")
      params.append(fts_query)
      snippet = (
          f"snippet(documents_fts, 2, '{_SNIPPET_START}', '{_SNIPPET_END}',"
          " '…', 24)"
      )
      order = f"bm25(documents_fts, {_BM25_WEIGHTS}), d.path"
    else:
      source = "documents d"
      snippet = "NULL"
      order = "d.path"
    if prefix:
      where.append("substr(d.path, 1, ?) = ?")
      params.extend([len(prefix), prefix])
    if wanted_tags:
      placeholders = ", ".join("?" for _ in wanted_tags)
      where.append(
          "d.id IN (SELECT document_id FROM document_tags"
          f" WHERE tag IN ({placeholders})"
          " GROUP BY document_id HAVING COUNT(*) = ?)"
      )
      params.extend(wanted_tags)
      params.append(len(wanted_tags))
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    with self._lock:
      total = self._conn.execute(
          f"SELECT COUNT(*) FROM {source}{where_sql}", params
      ).fetchone()[0]
      rows = self._conn.execute(
          "SELECT d.id, d.path, d.title, d.size_bytes, d.created_at,"
          f" d.updated_at, {snippet} AS snippet FROM {source}{where_sql}"
          f" ORDER BY {order} LIMIT ? OFFSET ?",
          [*params, limit, offset],
      ).fetchall()
      tags_by_id = self._tags_for([row["id"] for row in rows])
    documents = [
        Document(
            path=row["path"],
            title=row["title"],
            tags=tags_by_id.get(row["id"], []),
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            snippet_html=(
                None
                if row["snippet"] is None
                else _snippet_to_html(row["snippet"])
            ),
        )
        for row in rows
    ]
    return documents, total

  def documents(self, paths: Iterable[str] | None = None) -> list[Document]:
    """Returns whole documents (with content), sorted by path.

    Args:
      paths: Which documents to return; ``None`` returns all of them. Paths
        that do not exist are left out.
    """
    wanted = None
    if paths is not None:
      wanted = sorted({normalize_path(path) for path in paths})
    with self._lock:
      if wanted is None:
        rows = self._conn.execute(
            "SELECT * FROM documents ORDER BY path"
        ).fetchall()
      else:
        rows = []
        for start in range(0, len(wanted), 500):
          chunk = wanted[start : start + 500]
          placeholders = ", ".join("?" for _ in chunk)
          rows.extend(
              self._conn.execute(
                  "SELECT * FROM documents"
                  f" WHERE path IN ({placeholders}) ORDER BY path",
                  chunk,
              ).fetchall()
          )
      tags_by_id = self._tags_for([row["id"] for row in rows])
    return [
        Document(
            path=row["path"],
            title=row["title"],
            tags=tags_by_id.get(row["id"], []),
            size_bytes=row["size_bytes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            content=row["content"],
        )
        for row in rows
    ]

  def tags(self) -> list[dict]:
    """Returns all tags with the number of documents carrying each."""
    with self._lock:
      rows = self._conn.execute(
          "SELECT tag, COUNT(*) AS count FROM document_tags"
          " GROUP BY tag ORDER BY count DESC, tag"
      ).fetchall()
    return [{"tag": row["tag"], "count": row["count"]} for row in rows]

  # --- sync ----------------------------------------------------------------

  def changes(
      self,
      since: int = 0,
      limit: int = DEFAULT_CHANGES_LIMIT,
      exclude_origin: str | None = None,
      max_bytes: int = DEFAULT_CHANGES_BYTES,
  ) -> dict:
    """Returns the current state of every path changed after ``since``.

    Args:
      since: Feed position already seen by the caller (0 for everything).
      limit: Maximum number of changes to return.
      exclude_origin: Leave out changes that were received from the peer with
        this instance id, so they are not sent back to it.
      max_bytes: Stop adding changes once their content exceeds this size. At
        least one change is always returned.

    Returns:
      A dict with ``instance_id``, ``changes``, ``next_since`` (pass it as
      ``since`` next time) and ``more`` (whether another page is waiting).
    """
    since = max(0, int(since))
    limit = max(1, min(int(limit), MAX_CHANGES_LIMIT))
    origin_sql = ""
    origin_params: list = []
    if exclude_origin:
      origin_sql = " AND (origin IS NULL OR origin != ?)"
      origin_params = [exclude_origin]
    with self._lock:
      latest = self._latest_seq()
      rows = self._conn.execute(
          "SELECT seq, id, path, 0 AS deleted, updated_at AS at, created_at,"
          f" content FROM documents WHERE seq > ?{origin_sql}"
          " UNION ALL"
          " SELECT seq, NULL, path, 1, deleted_at, NULL, NULL"
          f" FROM tombstones WHERE seq > ?{origin_sql}"
          " ORDER BY seq LIMIT ?",
          [since, *origin_params, since, *origin_params, limit + 1],
      ).fetchall()
      more = len(rows) > limit
      rows = rows[:limit]
      size = 0
      for index, row in enumerate(rows):
        size += len(row["content"] or "")
        if size > max_bytes and index > 0:
          rows = rows[:index]
          more = True
          break
      tags_by_id = self._tags_for(
          [row["id"] for row in rows if row["id"] is not None]
      )
    changes = []
    for row in rows:
      change = {
          "path": row["path"],
          "deleted": bool(row["deleted"]),
          "updated_at": row["at"],
      }
      if not row["deleted"]:
        change["created_at"] = row["created_at"]
        change["content"] = row["content"]
        change["tags"] = tags_by_id.get(row["id"], [])
      changes.append(change)
    return {
        "instance_id": self.instance_id,
        "changes": changes,
        "next_since": rows[-1]["seq"] if more else latest,
        "more": more,
    }

  def apply_changes(self, changes, origin: str | None = None) -> dict:
    """Applies changes from another server; the newest version of a path wins.

    A change replaces the local state of its path only if it is newer. Equal
    timestamps are settled the same way on every server (a deletion beats a
    document, then the larger content hash wins), so servers converge.

    Args:
      changes: Entries as produced by ``changes()``.
      origin: Instance id of the server the changes came from.

    Returns:
      Counts of ``applied`` and ``skipped`` changes, and a ``rejected`` list
      describing entries that were invalid.
    """
    if not isinstance(changes, list):
      raise StoreError('"changes" must be a list')
    result = {"applied": 0, "skipped": 0, "rejected": []}
    for change in changes:
      try:
        with self._lock, self._conn:
          applied = self._apply_change(change, origin)
      except StoreError as e:
        path = change.get("path") if isinstance(change, dict) else None
        result["rejected"].append({"path": path, "error": str(e)})
        continue
      result["applied" if applied else "skipped"] += 1
    return result

  def get_peer(self, url: str) -> dict:
    """Returns what is remembered about syncing with the server at ``url``."""
    with self._lock:
      row = self._conn.execute(
          "SELECT instance_id, pull_cursor, push_cursor FROM sync_peers"
          " WHERE url = ?",
          (url,),
      ).fetchone()
    if row is None:
      return {"instance_id": None, "pull_cursor": 0, "push_cursor": 0}
    return dict(row)

  def save_peer(
      self, url: str, instance_id: str, pull_cursor: int, push_cursor: int
  ) -> None:
    with self._lock, self._conn:
      self._conn.execute(
          "INSERT INTO sync_peers (url, instance_id, pull_cursor, push_cursor)"
          " VALUES (?, ?, ?, ?) ON CONFLICT (url) DO UPDATE SET"
          " instance_id = excluded.instance_id,"
          " pull_cursor = excluded.pull_cursor,"
          " push_cursor = excluded.push_cursor",
          (url, instance_id, int(pull_cursor), int(push_cursor)),
      )

  def _apply_change(self, change, origin: str | None) -> bool:
    if not isinstance(change, dict):
      raise StoreError("each change must be an object")
    path = normalize_path(change.get("path"))
    when = change.get("updated_at")
    _parse_time(when)
    deleted = change.get("deleted", False)
    if not isinstance(deleted, bool):
      raise StoreError('"deleted" must be true or false')
    content = None
    tags: list[str] = []
    if not deleted:
      content = change.get("content")
      if not isinstance(content, str):
        raise StoreError('"content" must be a string')
      tags = normalize_tags(change.get("tags"))

    local = self._conn.execute(
        "SELECT id, updated_at AS at, content, 0 AS deleted FROM documents"
        " WHERE path = ? UNION ALL"
        " SELECT NULL, deleted_at, NULL, 1 FROM tombstones WHERE path = ?",
        (path, path),
    ).fetchone()
    if local is not None:
      theirs = (when, int(deleted))
      ours = (local["at"], local["deleted"])
      if theirs < ours:
        return False
      if theirs == ours:
        if deleted:
          return False
        local_tags = self._tags_for([local["id"]]).get(local["id"], [])
        if _digest(content, tags) <= _digest(local["content"], local_tags):
          return False

    if deleted:
      self._write_tombstone(path, when, origin)
    else:
      created_at = change.get("created_at")
      try:
        _parse_time(created_at)
      except StoreError:
        created_at = when
      self._write_document(
          path, content, tags, when, min(created_at, when), origin
      )
    return True

  # --- internals -----------------------------------------------------------

  def _latest_seq(self) -> int:
    return int(
        self._conn.execute(
            "SELECT value FROM meta WHERE key = 'seq'"
        ).fetchone()[0]
    )

  def _next_seq(self) -> int:
    return int(
        self._conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1"
            " WHERE key = 'seq' RETURNING value"
        ).fetchone()[0]
    )

  def _local_time(self, path: str) -> str:
    """Returns a timestamp for a local write that beats the current state.

    Normally that is the current time. If the path already carries a later
    timestamp (clocks differ between servers), the write is stamped just
    after it so that it is not lost on the next sync.
    """
    now = _now()
    row = self._conn.execute(
        "SELECT updated_at AS at FROM documents WHERE path = ? UNION ALL"
        " SELECT deleted_at FROM tombstones WHERE path = ?",
        (path, path),
    ).fetchone()
    if row is None or row["at"] < now:
      return now
    return _format_time(
        _parse_time(row["at"]) + datetime.timedelta(milliseconds=1)
    )

  def _write_document(
      self,
      path: str,
      content: str,
      tags: list[str] | None,
      updated_at: str,
      created_at: str,
      origin: str | None,
  ) -> bool:
    """Creates or replaces a document row; returns whether it was created.

    ``tags=None`` keeps the tags of an existing document.
    """
    title = derive_title(path, content)
    size_bytes = len(content.encode("utf-8"))
    seq = self._next_seq()
    self._conn.execute("DELETE FROM tombstones WHERE path = ?", (path,))
    row = self._conn.execute(
        "SELECT id FROM documents WHERE path = ?", (path,)
    ).fetchone()
    created = row is None
    if created:
      doc_id = self._conn.execute(
          "INSERT INTO documents (path, title, content, size_bytes,"
          " created_at, updated_at, seq, origin)"
          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (path, title, content, size_bytes, created_at, updated_at, seq,
           origin),
      ).lastrowid
    else:
      doc_id = row["id"]
      self._conn.execute(
          "UPDATE documents SET title = ?, content = ?, size_bytes = ?,"
          " updated_at = ?, seq = ?, origin = ? WHERE id = ?",
          (title, content, size_bytes, updated_at, seq, origin, doc_id),
      )
    if tags is not None:
      self._conn.execute(
          "DELETE FROM document_tags WHERE document_id = ?", (doc_id,)
      )
      self._conn.executemany(
          "INSERT INTO document_tags (document_id, tag) VALUES (?, ?)",
          [(doc_id, tag) for tag in tags],
      )
    stored_tags = self._tags_for([doc_id]).get(doc_id, [])
    self._conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
    self._conn.execute(
        "INSERT INTO documents_fts (rowid, path, title, content, tags)"
        " VALUES (?, ?, ?, ?, ?)",
        (doc_id, path, title, content, " ".join(stored_tags)),
    )
    return created

  def _write_tombstone(
      self, path: str, deleted_at: str, origin: str | None
  ) -> None:
    """Removes the document at ``path``, if any, and records the deletion."""
    row = self._conn.execute(
        "SELECT id FROM documents WHERE path = ?", (path,)
    ).fetchone()
    if row is not None:
      self._conn.execute(
          "DELETE FROM documents_fts WHERE rowid = ?", (row["id"],)
      )
      self._conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    self._conn.execute(
        "INSERT INTO tombstones (path, deleted_at, seq, origin)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (path) DO UPDATE SET"
        " deleted_at = excluded.deleted_at, seq = excluded.seq,"
        " origin = excluded.origin",
        (path, deleted_at, self._next_seq(), origin),
    )

  def _get_locked(self, path: str) -> Document:
    row = self._conn.execute(
        "SELECT id, path, title, content, size_bytes, created_at, updated_at"
        " FROM documents WHERE path = ?",
        (path,),
    ).fetchone()
    if row is None:
      raise NotFoundError(f"no document at {path!r}")
    return Document(
        path=row["path"],
        title=row["title"],
        tags=self._tags_for([row["id"]]).get(row["id"], []),
        size_bytes=row["size_bytes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        content=row["content"],
    )

  def _tags_for(self, doc_ids: list[int]) -> dict[int, list[str]]:
    result: dict[int, list[str]] = {}
    for start in range(0, len(doc_ids), 500):
      chunk = doc_ids[start : start + 500]
      placeholders = ", ".join("?" for _ in chunk)
      rows = self._conn.execute(
          "SELECT document_id, tag FROM document_tags"
          f" WHERE document_id IN ({placeholders}) ORDER BY tag",
          chunk,
      ).fetchall()
      for row in rows:
        result.setdefault(row["document_id"], []).append(row["tag"])
    return result
