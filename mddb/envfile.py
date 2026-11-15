"""Reads settings from a ``.env`` file into the process environment."""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse(text: str) -> dict[str, str]:
  """Parses ``NAME=value`` lines.

  Blank lines and lines starting with ``#`` are skipped. A leading ``export``
  is allowed. Values may be wrapped in single or double quotes; an unquoted
  value ends at a ``#`` that follows whitespace.
  """
  values = {}
  for line in text.splitlines():
    if not line.strip() or line.lstrip().startswith("#"):
      continue
    match = _LINE.match(line)
    if not match:
      continue
    name, value = match.group(1), match.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
      value = value[1:-1]
    else:
      value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    values[name] = value
  return values


def load(path: str | Path, environ=None) -> list[str]:
  """Sets variables from the file at ``path`` that are not already set.

  Variables already present in the environment are left alone, so a value
  given on the command line wins over the file.

  Returns:
    The names that were set. Empty if the file does not exist.
  """
  environ = os.environ if environ is None else environ
  try:
    text = Path(path).read_text(encoding="utf-8")
  except FileNotFoundError:
    return []
  loaded = []
  for name, value in parse(text).items():
    if name not in environ:
      environ[name] = value
      loaded.append(name)
  return loaded
