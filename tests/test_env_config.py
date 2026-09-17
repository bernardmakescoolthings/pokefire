import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pokefire.env_config import load_env


class EnvConfigTests(unittest.TestCase):
    @patch.dict(os.environ, {"EXISTING": "shell-value"}, clear=True)
    def test_quotes_comments_empty_values_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('# comment\nexport TOKEN="abc=123#456" # comment\n'
                            "EMPTY=\nEXISTING=file-value\nLITERAL='$(not-executed) $HOME'\n")
            load_env(path)
            self.assertEqual(os.environ["TOKEN"], "abc=123#456")
            self.assertEqual(os.environ["EMPTY"], "")
            self.assertEqual(os.environ["EXISTING"], "shell-value")
            self.assertEqual(os.environ["LITERAL"], "$(not-executed) $HOME")

    def test_missing_optional_and_explicit_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            load_env(path)
            with self.assertRaises(FileNotFoundError):
                load_env(path, required=True)

    @patch.dict(os.environ, {}, clear=True)
    def test_invalid_file_does_not_leak_values_or_partially_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('FIRST=value\nSECRET="private-secret\n')
            with self.assertRaises(ValueError) as caught:
                load_env(path)
            self.assertNotIn("private-secret", str(caught.exception))
            self.assertNotIn("FIRST", os.environ)
