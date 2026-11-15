"""Tests for reading settings from a .env file."""

import tempfile
import unittest
from pathlib import Path

from mddb import envfile


class EnvFileTest(unittest.TestCase):

  def test_parse(self):
    text = """
# a comment
PLAIN=value
export EXPORTED=yes
  SPACED = padded value   # trailing comment
DOUBLE="quoted # not a comment"
SINGLE='single quoted'
EMPTY=
WITH_EQUALS=a=b=c
HASH_INSIDE=abc#def
not a setting
1BAD=x
"""
    self.assertEqual(
        envfile.parse(text),
        {
            "PLAIN": "value",
            "EXPORTED": "yes",
            "SPACED": "padded value",
            "DOUBLE": "quoted # not a comment",
            "SINGLE": "single quoted",
            "EMPTY": "",
            "WITH_EQUALS": "a=b=c",
            "HASH_INSIDE": "abc#def",
        },
    )

  def test_load_does_not_override_existing_variables(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = Path(tmp) / ".env"
      path.write_text("NEW=from file\nEXISTING=from file\n")
      environ = {"EXISTING": "from environment"}
      self.assertEqual(envfile.load(path, environ), ["NEW"])
      self.assertEqual(
          environ, {"EXISTING": "from environment", "NEW": "from file"}
      )

  def test_missing_file_is_ignored(self):
    environ = {}
    self.assertEqual(envfile.load("/nonexistent/.env", environ), [])
    self.assertEqual(environ, {})


if __name__ == "__main__":
  unittest.main()
