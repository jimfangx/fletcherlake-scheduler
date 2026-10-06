"""Durable event consumer; failures and cancellation cannot alter hardware outcomes."""

import asyncio
import logging

import httpx
from fl_common.network import https_origin

from ..db.async_calls import database_call
from ..db.core import Database
from .claims import Claims, Delivery
from .config import NotificationSettings
from .outcomes import Outcome
from .projection import Projector
from .providers import providers

logger = logging.getLogger(__name__)


class Notifications:
    def __init__(
        self, db: Database, settings: NotificationSettings, client: httpx.AsyncClient, origin: str
    ) -> None:
        self.origin = https_origin(origin)
        self.providers = providers(settings, client)
        self.projector = Projector(db, settings, self.providers)
        self.claims = Claims(db, settings.max_attempts)

    async def deliver(self, delivery: Delivery) -> None:
        provider = self.providers.get(delivery.route_id)
        if provider is None:
            result = Outcome("SKIPPED", "DESTINATION_REMOVED")
        else:
            try:
                async with asyncio.timeout(20):
                    result = await provider.send(
                        delivery.notification_id, delivery.notice, self.origin
                    )
            except (httpx.TransportError, TimeoutError):
                result = Outcome("RETRY", "NETWORK_FAILURE")
            except Exception:
                # Provider exception strings may contain webhook credentials or response bytes.
                result = Outcome("FAILED", "PROVIDER_INTERNAL_ERROR")
        await database_call(self.claims.finish, delivery, result)

    async def tick(self) -> None:
        await database_call(self.projector.project)
        # Serial claims keep transactions small; independent destination IO can overlap.
        pending: list[Delivery] = []
        for _ in range(16):
            delivery = await database_call(self.claims.claim)
            if delivery is None:
                break
            pending.append(delivery)
        async with asyncio.TaskGroup() as group:
            for delivery in pending:
                group.create_task(self.deliver(delivery))

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Notification maintenance failed (%s)", type(error).__name__)
            await asyncio.sleep(1)
