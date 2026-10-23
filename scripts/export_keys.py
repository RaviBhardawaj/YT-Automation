"""Copy every API key the studio keeps in the macOS Keychain into secrets/api_keys.env (owner-only, git-ignored).

Run from the project folder:  .venv/bin/python scripts/export_keys.py
The app keeps reading keys from the Keychain; this file is only your own backup copy. Never commit or share it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import settings  # noqa: E402

out_dir = ROOT / "secrets"
out_dir.mkdir(mode=0o700, exist_ok=True)
os.chmod(out_dir, 0o700)
names = list(settings.SECRETS) + [f"google_refresh_token__ch{i}" for i in range(2, 51)]
lines = ["# API keys exported from the macOS Keychain. Private: git-ignored, owner-only.", ""]
saved = []
for name in names:
    value = settings.secret(name)
    if value:
        label = settings.SECRETS.get(name, {}).get("label") or f"YouTube login for studio channel {name.rsplit('ch', 1)[-1]}"
        lines += [f"# {label}", f"{name.upper()}={value}", ""]
        saved.append(name)
path = out_dir / "api_keys.env"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(lines))
os.chmod(path, 0o600)
print(f"Saved {len(saved)} key(s) to {path}: {', '.join(saved) or 'none'}")
print("Not set:", ", ".join(n for n in settings.SECRETS if n not in saved) or "none")
