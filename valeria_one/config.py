"""Settings come from the environment (or a .env file next to the project). Nothing secret lives in code."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def get(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"{name} is not set")
    return v


DATABASE_URL = lambda: get("VO_DATABASE_URL", required=True)            # noqa: E731
PUBLIC_URL = lambda: (get("VO_PUBLIC_URL") or "http://localhost:8000").rstrip("/")  # noqa: E731
SANDBOX = lambda: get("VO_SANDBOX", "0") == "1"                          # noqa: E731  sandbox instance: fake channels only
