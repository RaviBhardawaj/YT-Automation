"""Load keys from secrets/api_keys.env into this computer's secure key store
(macOS Keychain or Windows Credential Manager). Used when moving the studio to another PC.

Run from the project folder:  .venv/bin/python scripts/import_keys.py   (Windows: .venv\\Scripts\\python.exe)
Afterwards you can delete secrets/api_keys.env; the app reads keys from the key store.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import settings  # noqa: E402

path = ROOT / "secrets" / "api_keys.env"
if not path.exists():
    sys.exit(f"{path} not found — copy it from the old computer first (scripts/export_keys.py makes it).")
loaded, skipped = [], []
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    name, value = name.strip().lower(), value.strip()
    if not value:
        continue
    if not settings.known_secret(name):
        skipped.append(name)
        continue
    settings.set_secret(name, value)
    loaded.append(name)
print(f"Loaded {len(loaded)} key(s) into the key store: {', '.join(loaded) or 'none'}")
if skipped:
    print("Skipped unknown names:", ", ".join(skipped))
