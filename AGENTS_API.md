# Using mddb from an agent

mddb is a store for markdown documents. Use its REST API to save `.md` files
(skills, summaries, instructions, notes) and to find them again by path, tag
or full-text search.

## Before you start

- **Base URL**: `http://127.0.0.1:8420` unless you were told another one. The
  examples use `$MDDB_URL`:

  ```bash
  export MDDB_URL=http://127.0.0.1:8420
  ```

- **Check that it is up**: `curl -s $MDDB_URL/api/health` returns
  `{"status": "ok"}`. If the connection is refused, the server is not running;
  it is started with `python3 -m mddb` in the mddb directory.
- **Token**: reads never need one. If writes return `401`, the server was
  started with a token. Add `-H "Authorization: Bearer $MDDB_TOKEN"` to every
  `PUT`, `POST` and `DELETE`.

## Documents

A document has:

| Field | Meaning |
|---|---|
| `path` | Its identity, like a file path: `skills/python/testing.md`. |
| `content` | The markdown text. |
| `tags` | Lowercase labels used for filtering, e.g. `["skill", "python"]`. |
| `title` | Set by the server: the first `# heading`, else the file name. |
| `size_bytes`, `created_at`, `updated_at` | Set by the server. Times are UTC. |

Rules for paths:

- Must end in `.md`. Use `/` between folders. No leading `/` is needed.
- No `..`, `.` or empty segments, no backslashes, at most 512 characters.
- In a URL, percent-encode each segment (a space becomes `%20`). Do not encode
  the `/` separators.

Rules for tags:

- Lowercased and trimmed by the server. At most 64 tags of 64 characters each.
- A tag cannot contain a comma.

Suggested layout, so that documents are easy to find: put the kind of document
first in the path (`skills/…`, `summaries/…`, `instructions/…`) and use tags
for topics (`python`, `testing`, `release`).

## Store a document

Writing to a path creates the document, or replaces it if it already exists.
There is no separate update call.

**Raw markdown** (simplest; no escaping needed). Tags go in the query string:

```bash
curl -sS -X PUT "$MDDB_URL/api/documents/skills/python/testing.md?tags=skill,python" \
  -H 'Content-Type: text/markdown' \
  --data-binary @testing.md
```

Use `--data-binary`, not `-d`; `-d` strips the line breaks from the file.

**JSON**:

```bash
curl -sS -X PUT "$MDDB_URL/api/documents/summaries/2026-11-15-standup.md" \
  -H 'Content-Type: application/json' \
  -d '{"content": "# Standup\n\n- shipped search", "tags": ["summary", "standup"]}'
```

`POST $MDDB_URL/api/documents` takes the same JSON with an extra `"path"`
field, for when you would rather not build the URL.

The response is the stored document as JSON. The status is `201` if it was
created and `200` if it replaced an existing document.

What happens to tags when you replace a document:

| You send | Result |
|---|---|
| No tags (no `tags` in the query string or JSON) | Existing tags are kept. |
| A list of tags | The list replaces the existing tags. |
| An empty list (`"tags": []`, JSON only) | All tags are removed. |

To add one tag, fetch the document, then send it back with the full new list.

Before you overwrite, check whether the path is already used
(`GET /api/documents/<path>` returns `404` if it is free). A replaced document
cannot be recovered.

## Find documents

`GET /api/documents` lists documents. Filters can be combined; a document must
satisfy all of them.

| Parameter | Meaning |
|---|---|
| `q` | Full-text search over path, title, content and tags. |
| `tag` | Only documents with this tag. Repeat it, or use `tags=a,b`, to require several. |
| `prefix` | Only paths that start with this string, e.g. `skills/`. |
| `limit` | Page size. Default 500, at most 5000. |
| `offset` | Number of documents to skip. |

```bash
# Search everything
curl -sS "$MDDB_URL/api/documents?q=canary+rollout"

# All documents tagged both skill and python
curl -sS "$MDDB_URL/api/documents?tag=skill&tag=python"

# Search inside one folder
curl -sS "$MDDB_URL/api/documents?prefix=skills/&q=testing"

# Which tags exist, most used first
curl -sS "$MDDB_URL/api/tags"
```

Response:

```json
{
  "documents": [
    {
      "path": "skills/python/testing.md",
      "title": "Testing in Python",
      "tags": ["python", "skill"],
      "size_bytes": 1840,
      "created_at": "2026-11-15T19:25:48.938Z",
      "updated_at": "2026-11-15T19:25:48.938Z",
      "snippet_html": "Run the <mark>canary</mark> first…"
    }
  ],
  "total": 1
}
```

- Listings do **not** include `content`. Fetch the document to read it.
- `total` counts all matches, which can be more than the page you received.
- `snippet_html` appears only when you search with `q`. It is a short excerpt
  of the content with the matched words wrapped in `<mark>`.
- Without `q`, results are sorted by path. With `q`, the best match is first.

How `q` matches:

- Case does not matter. Punctuation is ignored and splits words.
- Every word must appear somewhere in the document.
- A word also matches longer words that start with it: `deploy` finds
  `deployment`. It does not match the middle or end of a word.
- There are no operators. `OR`, quotes and `-word` are treated as plain words
  or ignored. To search for alternatives, make one request per alternative.

## Read a document

```bash
# JSON, with "content" and all metadata
curl -sS "$MDDB_URL/api/documents/skills/python/testing.md"

# Just the markdown text
curl -sS "$MDDB_URL/api/documents/skills/python/testing.md?raw=1"
```

## Delete a document

```bash
curl -sS -X DELETE "$MDDB_URL/api/documents/skills/python/testing.md"
```

Returns `204` with no body. Deletion is permanent. Only delete documents you
created or were asked to remove.

## If the store is synced with another server

The server you talk to may mirror its documents to a second server. You do not
need to do anything for that: use the API as described above. Two effects are
worth knowing:

- A document you just stored appears on the other server after the next sync
  (about a minute by default), not immediately.
- If the same path is changed on both servers in that window, the later change
  replaces the other one. To avoid losing work, give new documents unique
  paths rather than reusing a shared one.

## Errors

Errors have a JSON body: `{"error": "what went wrong"}`.

| Status | Meaning | What to do |
|---|---|---|
| `400` | Bad path, tags, JSON or parameter. | Read the message and fix the request. |
| `401` | Write without a valid token. | Send the bearer token. |
| `404` | No document at that path, or unknown endpoint. | Check the path; list with `prefix` to see what exists. |
| `405` | Method not allowed on that URL. | Check the method and URL. |
| `413` | Body larger than the limit (10 MiB by default). | Split the document. |
| `415` | Wrong `Content-Type`. | Use `text/markdown` or `application/json`. |

## Python example

Standard library only:

```python
import json
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8420"


def _url(path):
    return f"{BASE}/api/documents/{urllib.parse.quote(path)}"


def store(path, markdown, tags=None):
    """Creates or replaces a document. tags=None keeps existing tags."""
    body = {"content": markdown}
    if tags is not None:
        body["tags"] = tags
    request = urllib.request.Request(
        _url(path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def find(query=None, tags=(), prefix=None):
    """Returns matching documents (metadata only, no content)."""
    params = [("tag", tag) for tag in tags]
    if query:
        params.append(("q", query))
    if prefix:
        params.append(("prefix", prefix))
    url = f"{BASE}/api/documents?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url) as response:
        return json.load(response)["documents"]


def read(path):
    """Returns the markdown text of one document."""
    with urllib.request.urlopen(_url(path) + "?raw=1") as response:
        return response.read().decode()


store("notes/hello.md", "# Hello\n\nFirst note.", ["notes"])
for doc in find(query="hello", tags=["notes"]):
    print(doc["path"], "-", doc["title"])
    print(read(doc["path"]))
```

A failed request raises `urllib.error.HTTPError`; `error.code` is the status
and `json.load(error)` gives the `{"error": ...}` body.
