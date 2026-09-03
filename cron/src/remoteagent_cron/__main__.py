from __future__ import annotations

import logging

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "remoteagent_cron.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=True,
    )


if __name__ == "__main__":
    main()
