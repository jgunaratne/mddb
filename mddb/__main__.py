"""Command line entry point: ``python3 -m mddb``."""

from __future__ import annotations

import argparse
import logging
import os
import urllib.parse
from pathlib import Path

from mddb import chat as chat_lib
from mddb import server as server_lib
from mddb import store as store_lib
from mddb import sync as sync_lib

_REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
  parser = argparse.ArgumentParser(
      prog="mddb", description="Local markdown document server."
  )
  parser.add_argument(
      "--host",
      default=os.environ.get("MDDB_HOST", "127.0.0.1"),
      help="Address to listen on. Use '::' to accept connections from other"
      " machines (default: %(default)s).",
  )
  parser.add_argument(
      "--port",
      type=int,
      default=int(os.environ.get("MDDB_PORT", "8420")),
      help="Port to listen on (default: %(default)s).",
  )
  parser.add_argument(
      "--db",
      default=os.environ.get("MDDB_DB", str(_REPO_ROOT / "data" / "mddb.sqlite3")),
      help="SQLite database file (default: %(default)s).",
  )
  parser.add_argument(
      "--token",
      default=os.environ.get("MDDB_TOKEN"),
      help="If set, writes (PUT/POST/DELETE) need"
      " 'Authorization: Bearer <token>'. Also read from $MDDB_TOKEN.",
  )
  parser.add_argument(
      "--max-body-mb",
      type=float,
      default=10,
      help="Largest accepted request body in MiB (default: %(default)s).",
  )
  parser.add_argument(
      "--gemini-model",
      default=os.environ.get("MDDB_GEMINI_MODEL", chat_lib.DEFAULT_MODEL),
      help="Gemini model used by the chat panel (default: %(default)s). Chat"
      " is enabled when $GEMINI_API_KEY is set.",
  )
  parser.add_argument(
      "--chat-context-chars",
      type=int,
      default=chat_lib.DEFAULT_MAX_CONTEXT_CHARS,
      help="Most document text, in characters, given to the model per"
      " question (default: %(default)s).",
  )
  parser.add_argument(
      "--sync-url",
      default=os.environ.get("MDDB_SYNC_URL"),
      help="Base URL of another mddb server to keep in sync with, e.g."
      " https://notes.example.com. This server connects to it; the other"
      " server needs no sync settings.",
  )
  parser.add_argument(
      "--sync-token",
      default=os.environ.get("MDDB_SYNC_TOKEN"),
      help="The other server's write token, if it has one. Also read from"
      " $MDDB_SYNC_TOKEN.",
  )
  parser.add_argument(
      "--sync-interval",
      type=float,
      default=float(os.environ.get("MDDB_SYNC_INTERVAL", "60")),
      help="Seconds between syncs (default: %(default)s).",
  )
  parser.add_argument(
      "--sync-once",
      action="store_true",
      help="Sync with --sync-url once, print the result and exit without"
      " serving.",
  )
  args = parser.parse_args()
  if args.sync_once and not args.sync_url:
    parser.error("--sync-once needs --sync-url")
  if args.sync_interval < 1:
    parser.error("--sync-interval must be at least 1 second")

  logging.basicConfig(
      level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
  )
  store = store_lib.Store(args.db)
  syncer = None
  if args.sync_url:
    try:
      syncer = sync_lib.Syncer(store, args.sync_url, token=args.sync_token)
    except ValueError as e:
      parser.error(str(e))
    remote_host = urllib.parse.urlsplit(syncer.remote_url).hostname
    if (
        args.sync_token
        and syncer.remote_url.startswith("http://")
        and remote_host not in ("127.0.0.1", "localhost", "::1")
    ):
      logging.warning(
          "--sync-url is plain http: the sync token and all documents are"
          " sent unencrypted"
      )
  if args.sync_once:
    try:
      print(syncer.run())
    except sync_lib.SyncError as e:
      raise SystemExit(f"sync failed: {e}") from None
    finally:
      store.close()
    return

  chat = None
  api_key = os.environ.get("GEMINI_API_KEY", "").strip()
  if api_key:
    chat = chat_lib.Chat(
        api_key,
        model=args.gemini_model,
        max_context_chars=args.chat_context_chars,
    )
  httpd = server_lib.Server(
      (args.host, args.port),
      store,
      token=args.token,
      max_body_bytes=int(args.max_body_mb * 1024 * 1024),
      syncer=syncer,
      chat=chat,
  )
  shown_host = f"[{args.host}]" if ":" in args.host else args.host
  logging.info("database: %s", args.db)
  logging.info("serving on http://%s:%d", shown_host, httpd.server_port)
  if chat:
    logging.info("chat: enabled, model %s", chat.model)
  else:
    logging.info("chat: disabled (set GEMINI_API_KEY to enable it)")
  if args.host not in ("127.0.0.1", "localhost", "::1") and not args.token:
    logging.warning(
        "listening on a non-loopback address without --token: anyone who can"
        " reach this port can add, replace and delete documents"
    )
  worker = None
  if syncer:
    logging.info(
        "syncing with %s every %g seconds", syncer.remote_url, args.sync_interval
    )
    worker = sync_lib.SyncWorker(syncer, args.sync_interval)
    worker.start()
  try:
    httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    if worker:
      worker.stop()
      worker.join(timeout=5)
    httpd.server_close()
    store.close()


if __name__ == "__main__":
  main()
