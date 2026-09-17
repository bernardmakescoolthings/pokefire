"""Load simple KEY=value files without executing shell code or expanding values."""

import os
from pathlib import Path
import re
import shlex


def load_env(path, required=False):
    path = Path(path)
    if not path.exists() and not required:
        return
    values = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env assignment on line {number}")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"Invalid .env quoting on line {number}") from None
        if len(parts) > 1:
            raise ValueError(f"Quote values containing spaces in .env on line {number}")
        values[key] = parts[0] if parts else ""
    # Existing environment variables win, including deliberately empty values.
    for key, value in values.items():
        os.environ.setdefault(key, value)
