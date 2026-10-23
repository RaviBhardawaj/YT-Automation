"""Run the studio: `python -m studio` (serves http://127.0.0.1:8765)."""
from __future__ import annotations

import uvicorn

from . import config


def main() -> None:
    uvicorn.run("studio.web.app:app", host=config.HOST, port=config.PORT, log_level="warning",
                proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
