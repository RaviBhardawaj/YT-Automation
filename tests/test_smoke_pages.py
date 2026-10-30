"""Every dashboard page opens for a signed-in owner without a server error (a broken page = a dead menu item).
Pages with an id in the URL are opened with a real row where one exists. Nothing external is called.
Run: .venv/bin/python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-test-"))
os.environ.setdefault("STUDIO_KEYRING_SERVICE", "yt-automation-studio-tests")  # never the real Keychain logins
os.environ.setdefault("STUDIO_OFFLINE", "1")              # never call the free image services

from studio import config, db  # noqa: E402

config.ensure_dirs()
db.migrate()
from studio import auth, channels  # noqa: E402


class PageTests(unittest.TestCase):
    def test_every_page_opens(self):
        from fastapi.testclient import TestClient

        from studio.web import app as webapp
        if not db.q1("SELECT id FROM users WHERE email='smoke@test.local'"):
            auth.approve(auth.register("smoke@test.local", "a long enough password")["user_id"], 1)
        db.ex("UPDATE users SET is_admin=1 WHERE email='smoke@test.local'")
        tok, _ = auth.login("smoke@test.local", "a long enough password")
        ws = auth.session(tok)["workspace"]["id"]
        cid = channels.create({"name": f"Smoke {os.urandom(2).hex()}"}, ws)
        pid = db.insert("productions", {"format": "long", "title": "Smoke video", "status": "uploaded", "stage": "qa",
                                        "kind": "general", "workspace_id": ws, "channel_id": cid,
                                        "youtube_video_id": "smoke1", "package": json.dumps({"title": "t", "scenes": []}),
                                        "qa": json.dumps({"passed": True, "checks": []}), "timeline": "{}",
                                        "created_at": db.iso(), "updated_at": db.iso()})
        c = TestClient(webapp.app, base_url=f"http://127.0.0.1:{config.PORT}", follow_redirects=False,
                       raise_server_exceptions=False)
        c.cookies.set("sid", tok)
        ids = {"pid": pid, "cid": cid, "channel_id": cid, "id": pid}
        broken, opened = [], 0
        for route in webapp.app.routes:
            path = getattr(route, "path", "")
            if "GET" not in getattr(route, "methods", set()) or path.startswith(("/api", "/static", "/media",
                                                                                   "/download", "/oauth", "/auth")):
                continue
            url = path
            for k, v in ids.items():
                url = url.replace("{%s}" % k, str(v)).replace("{%s:int}" % k, str(v))
            if "{" in url:
                continue                                    # an id kind this smoke test has no row for
            r = c.get(url)
            opened += 1
            if r.status_code >= 500:
                broken.append(f"{url} → {r.status_code}")
        self.assertGreater(opened, 30)
        self.assertEqual(broken, [], "pages that crash")


if __name__ == "__main__":
    unittest.main()
