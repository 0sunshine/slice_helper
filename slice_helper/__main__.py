from __future__ import annotations

import uvicorn
from dotenv import load_dotenv

from .config import Settings


def main() -> None:
    load_dotenv(override=False)
    settings = Settings.from_env()
    uvicorn.run(
        "slice_helper.app:app",
        host=settings.host,
        port=settings.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
