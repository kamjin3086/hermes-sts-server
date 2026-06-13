from __future__ import annotations

import uvicorn

from hermes_sts.config import settings


def main() -> None:
    uvicorn.run(
        "hermes_sts.server:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
