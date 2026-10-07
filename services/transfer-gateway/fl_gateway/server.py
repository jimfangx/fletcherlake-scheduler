"""Linux HTTP service; HTTPS proxies expose verification publicly and control privately."""

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from .api import create_app
from .maintenance import sweep
from .store import GatewayStore

logger = logging.getLogger(__name__)


async def maintain(store: GatewayStore) -> None:
    while True:
        operation = asyncio.create_task(asyncio.to_thread(sweep, store))
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise
        except Exception as error:
            logger.error("Transfer maintenance failed (%s)", type(error).__name__)
        await asyncio.sleep(5)


def application() -> FastAPI:
    credential = Path(os.environ["FL_GATEWAY_CONTROL_SECRET_FILE"])
    info = credential.stat()
    if credential.is_symlink() or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise PermissionError("Gateway control credential requires this user's mode-0600 file")
    secret = SecretStr(credential.read_text().strip())
    store = GatewayStore(Path(os.environ["FL_GATEWAY_ROOT"]))
    sweep(store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(maintain(store))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return create_app(store, secret, lifespan=lifespan)


def main() -> None:
    uvicorn.run(
        "fl_gateway.server:application",
        factory=True,
        host=os.environ.get("FL_BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("FL_BIND_PORT", "8081")),
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
