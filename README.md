# mddb

A small local server that stores markdown (`.md`) documents in a SQLite
database. It has:

- a **web app** to browse documents in a file tree, filter by tag, search the
  full text, and read them rendered or raw;
- a **REST API** to send documents to the server and to query them;
- **two-way sync** with a second mddb server, so the same documents are
  available in two places;
- a **chat panel** that answers questions about the documents using a Gemini
  model.

Documents can be anything written in markdown: skills, summaries, instructions,
notes. Each one has a path such as `skills/python/testing.md` and any number of
tags.

It needs Python 3.11 or newer and nothing else.

## Run

```bash
cd ~/git/mddb
python3 -m mddb
```

Then open <http://127.0.0.1:8420>.

| Flag | Environment | Default | Meaning |
|---|---|---|---|
| `--host` | `MDDB_HOST` | `127.0.0.1` | Address to listen on. |
| `--port` | `MDDB_PORT` | `8420` | Port to listen on. |
| `--db` | `MDDB_DB` | `data/mddb.sqlite3` | SQLite database file. |
| `--token` | `MDDB_TOKEN` | none | Require this bearer token for writes. |
| `--max-body-mb` | | `10` | Largest accepted request body. |
| | `GEMINI_API_KEY` | none | Gemini API key. Setting it turns the chat panel on. |
| `--gemini-model` | `MDDB_GEMINI_MODEL` | `gemini-4-argon` | Model used by chat. |
| `--chat-context-chars` | | `2000000` | Most document text given to the model per question. |
| `--sync-url` | `MDDB_SYNC_URL` | none | Another mddb server to sync with. |
| `--sync-token` | `MDDB_SYNC_TOKEN` | none | That server's write token. |
| `--sync-interval` | `MDDB_SYNC_INTERVAL` | `60` | Seconds between syncs. |
| `--sync-once` | | | Sync once and exit, without serving. |

Settings can also be kept in a `.env` file in the mddb directory, one
`NAME=value` per line, using the names from the Environment column:

```bash
GEMINI_API_KEY=your-key
MDDB_TOKEN=choose-a-secret
```

The file is read at startup. A variable that is already set in the environment
wins over the file, and a flag wins over both. `.env` is listed in
`.gitignore`, so keys in it are not committed.

By default only programs on the same machine can connect. To reach the server
from another machine, listen on all addresses and set a token so that others
cannot change your documents:

```bash
MDDB_TOKEN=choose-a-secret python3 -m mddb --host ::
```

Reads stay open to anyone who can reach the port; the token only guards writes.

## Chat with your documents

The panel on the right of the web app answers questions about the stored
documents. It needs a Gemini API key (from <https://aistudio.google.com/apikey>)
when the server starts, either in the environment or in the `.env` file:

```bash
GEMINI_API_KEY=your-key python3 -m mddb
```

How it works:

- Each question is sent to the Gemini API together with the full text of the
  documents chosen under **Context**: all documents, the documents you
  selected, the documents currently in the list (after search and tag
  filters), or only the open document.
- **Selecting documents**: tick the checkbox next to any files in the list, or
  next to a folder to take everything in it. Search results have checkboxes
  too, and an open document has an **Add to chat** button. Ticking the first
  file switches Context to **Selected documents**. The chat panel lists the
  selection, where single files can be removed or the whole selection cleared.
  The selection is remembered in the browser.
- The answer is streamed back and names the documents it used; those links
  open the document in the viewer.
- The conversation lives in the browser tab. **New chat** or a page reload
  starts over. Nothing about the chat is stored in the database.

Things to know:

- **Your documents leave this machine**: everything in the chosen context is
  sent to Google's Gemini API with every question.
- **Cost**: the whole context is sent again with each question, so a large
  store makes every question a large request. Narrow the context to keep
  requests small.
- **Size limit**: at most `--chat-context-chars` characters of documents are
  sent (about 500,000 tokens by default). Documents that do not fit are left
  out, and the panel says how many.
- **Token**: if the server was started with `--token`, the chat asks for that
  token once and remembers it in the browser. This stops other people who can
  reach the server from spending your API quota.
- Without `GEMINI_API_KEY` the panel explains that chat is off; everything
  else works as before.

## Sync between two servers

Two mddb servers can mirror each other. Both run this same code. Only one of
them needs to be able to reach the other: that one is started with `--sync-url`
and does all the connecting. The other one needs no sync settings.

On the server that is reachable (for example a machine on the internet), put it
behind HTTPS and give it a token:

```bash
MDDB_TOKEN=choose-a-secret python3 -m mddb --host ::
```

On the server that connects out (for example your own machine):

```bash
MDDB_SYNC_TOKEN=choose-a-secret python3 -m mddb --sync-url https://notes.example.com
```

It syncs when it starts and then every 60 seconds. Each sync pulls the other
server's changes and pushes its own, so documents, tags and deletions travel in
both directions. Only what changed since the last sync is transferred.

Other ways to run it:

```bash
# Sync right now (on the server started with --sync-url)
curl -X POST http://127.0.0.1:8420/api/sync

# When it last synced, and whether that worked
curl http://127.0.0.1:8420/api/sync

# Sync once from the command line and exit, e.g. from cron
python3 -m mddb --sync-url https://notes.example.com --sync-once
```

Things to know:

- **Conflicts**: if the same path was changed on both servers between two
  syncs, the change that was written last wins on both, and the other one is
  discarded. This includes deletions: a delete that is newer than an edit wins,
  and the other way round. Nothing is merged.
- **Clocks**: "written last" compares the clocks of the two machines, so keep
  them roughly right (NTP is enough).
- **Use HTTPS** for `--sync-url` when the other server is on another machine.
  Over plain `http` the token and the documents are sent unencrypted. mddb
  itself only speaks HTTP, so put the reachable server behind a reverse proxy
  that adds TLS.
- **Deleted paths are remembered** in the database, so that a deletion can be
  passed on. These records are small and are kept indefinitely.
- If the server at `--sync-url` is replaced by a new, empty database, the next
  sync fills it again with everything from this side.
- A server can sync with only one `--sync-url`, but it can also be the target
  of other servers, so more than two can be chained.

## API

All responses are JSON unless noted. Errors look like `{"error": "..."}`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/documents` | List or search documents (no content). |
| `GET` | `/api/documents/<path>` | Fetch one document. |
| `PUT` | `/api/documents/<path>` | Create or replace a document. |
| `POST` | `/api/documents` | Same as `PUT`, with the path in the body. |
| `DELETE` | `/api/documents/<path>` | Delete a document. |
| `GET` | `/api/tags` | All tags with document counts. |
| `GET` | `/api/health` | Liveness check. |
| `GET` | `/api/chat` | Whether chat is enabled, and the model. |
| `POST` | `/api/chat` | Ask a question; the answer is streamed. Used by the web app. |
| `GET` | `/api/sync` | Sync status of this server. |
| `POST` | `/api/sync` | Sync with `--sync-url` now. |
| `GET`, `POST` | `/api/sync/changes` | Used by servers to exchange changes. |

### Send a document

Raw markdown, with tags in the query string:

```bash
curl -X PUT 'http://127.0.0.1:8420/api/documents/skills/python/testing.md?tags=skill,python' \
  -H 'Content-Type: text/markdown' \
  --data-binary @testing.md
```

Or JSON:

```bash
curl -X PUT http://127.0.0.1:8420/api/documents/notes/todo.md \
  -H 'Content-Type: application/json' \
  -d '{"content": "# Todo\n\n- write docs", "tags": ["notes"]}'

curl -X POST http://127.0.0.1:8420/api/documents \
  -H 'Content-Type: application/json' \
  -d '{"path": "notes/todo.md", "content": "# Todo", "tags": ["notes"]}'
```

The response is the stored document, with status `201` when it was created and
`200` when it replaced an existing one.

- Paths must end in `.md`, use `/` as the separator, and cannot contain `..`.
- Tags are lowercased and trimmed.
- When you replace a document without giving tags, its existing tags are kept.
  Send an empty list (`"tags": []`) to remove them.
- The title is the first `# heading`, or the file name if there is none.
- With `--token`, add `-H 'Authorization: Bearer <token>'` to writes.

### Query documents

```bash
# Everything
curl http://127.0.0.1:8420/api/documents

# Full-text search, best matches first, with a highlighted snippet
curl 'http://127.0.0.1:8420/api/documents?q=canary+rollout'

# Documents that have all of the given tags
curl 'http://127.0.0.1:8420/api/documents?tag=skill&tag=python'

# Everything under a folder, combined with search
curl 'http://127.0.0.1:8420/api/documents?prefix=skills/&q=testing'

# One document as JSON, or as raw markdown
curl http://127.0.0.1:8420/api/documents/skills/python/testing.md
curl 'http://127.0.0.1:8420/api/documents/skills/python/testing.md?raw=1'
```

| Parameter | Meaning |
|---|---|
| `q` | Search words. Every word must match the path, title, content or tags. A word also matches longer words that start with it. |
| `tag` | Repeatable; `tags=a,b` also works. All tags must be present. |
| `prefix` | Only paths that start with this string. |
| `limit`, `offset` | Paging. `limit` defaults to 500, at most 5000. |

A listing returns `{"documents": [...], "total": N}`. Each entry has `path`,
`title`, `tags`, `size_bytes`, `created_at` and `updated_at`; search results
also have `snippet_html`. Fetch a single document to get its `content`.

## Web app

- **Files**: folders and files are built from the document paths.
- **Tags**: click tags to show only documents that have all of them.
- **Search**: type in the search box (or press `/` first). Results are ranked
  and show where the words matched. Press Enter to open the best match.
- **Viewer**: rendered markdown or the raw text, plus a download link. Links
  between documents (`[other](../other.md)`) open inside the app.

- **Chat**: ask questions about the documents; see below.

The web app does not edit documents. They are added, changed and deleted
through the API.

## How it is built

| Part | Location | Notes |
|---|---|---|
| Storage | `mddb/store.py` | SQLite. Tables `documents` and `document_tags`, plus an FTS5 index for search. Every write gets a position in a change feed; deletions are kept in `tombstones`. |
| Chat | `mddb/chat.py`, `web/chat.js` | Puts the documents in the model's context and streams the answer from the Gemini API. |
| Sync | `mddb/sync.py` | Pulls the other server's change feed and pushes this one's, remembering how far it got. |
| HTTP server | `mddb/server.py` | Python standard library `http.server`, one thread per connection. |
| Web app | `web/` | Plain HTML, CSS and JavaScript. No build step. |
| Markdown rendering | `web/vendor/` | `marked` 16.0.0 renders in the browser; `DOMPurify` 3.2.6 removes scripts and other unsafe HTML. Vendored copies; licences are alongside (marked: MIT, DOMPurify: Apache-2.0 or MPL-2.0). |

The database is a single file, `data/mddb.sqlite3` by default. Copy that file
to back everything up.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```
