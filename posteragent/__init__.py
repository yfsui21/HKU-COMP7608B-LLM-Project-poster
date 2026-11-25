"""Package init: load credentials from an untracked local env file.

Keeping the key in `.env.local` (gitignored) rather than in config.py means
`git add -A` can never commit it.

`.env.local` takes precedence over the inherited environment, which is the
opposite of the usual convention but is the only useful behaviour here: a shell
that already exports a key for a different endpoint would otherwise silently
win and the file would look like it had no effect. Set POSTER_ENV_OVERRIDE=0
to flip it back.
"""
from __future__ import annotations

import os
from pathlib import Path

# POSTER_ENV_FILE points at a credentials file kept elsewhere, so the key
# never has to be copied into the working tree.
_ENV_FILE = Path(os.environ.get("POSTER_ENV_FILE")
                 or Path(__file__).resolve().parent.parent / ".env.local")


def _load_env_file(path: Path = _ENV_FILE) -> list[str]:
    """Minimal KEY=VALUE reader. Returns the names it set."""
    if not path.exists():
        return []
    override = os.environ.get("POSTER_ENV_OVERRIDE", "1") != "0"
    applied: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if not key:
            continue
        if override or not os.environ.get(key):
            os.environ[key] = val
            applied.append(key)
    return applied


LOADED_ENV = _load_env_file()
