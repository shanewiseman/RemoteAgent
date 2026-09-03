from __future__ import annotations

import uvicorn

from .config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "remoteagent.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
