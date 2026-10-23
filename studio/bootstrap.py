"""One-time process initialisation shared by the web app, CLI and tests."""
from __future__ import annotations

import logging

from . import config, db, logs

_done = False


def init(start_background: bool = False) -> None:
    global _done
    if not _done:
        config.ensure_dirs()
        logs.setup()
        db.migrate()
        from . import net
        net.configure_ip_preference()
        from .finance import marketdata, rules
        rules.seed()
        marketdata.seed_sources()
        from . import channels
        channels.ensure_default()           # the original setup becomes studio channel #1 (with its history)
        from . import scheduler, tasks  # noqa: F401  (registers task handlers)
        from .media import tts
        try:
            tts.ensure_espeak_data()
        except Exception:
            logging.getLogger(__name__).exception("could not prepare espeak data")
        scheduler.ensure_defaults()
        scheduler.sync_channels()
        _done = True
    if start_background:
        from . import scheduler, worker
        worker.start()
        scheduler.start()
