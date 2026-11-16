"""Start the API server.

On Windows, psycopg's async driver needs a SelectorEventLoop (the default Proactor loop
is not supported), so we run uvicorn's server inside an explicit selector loop.
"""

import asyncio
import os
import sys

import uvicorn


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if sys.platform == "win32":
        config = uvicorn.Config("app.main:app", host=host, port=port, log_level="info")
        server = uvicorn.Server(config)
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        uvicorn.run("app.main:app", host=host, port=port, reload="--reload" in sys.argv)


if __name__ == "__main__":
    main()
