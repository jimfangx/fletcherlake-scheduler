"""Brief membership cache. Profiles in PostgreSQL never grant access."""

import asyncio
import time
from dataclasses import dataclass

from fl_common.errors import PlatformError
from fl_common.models.scheduler import Principal, Role

from .google import GroupDirectory, Identity


@dataclass(frozen=True)
class GroupSettings:
    users: str
    operators: str | None = None
    admins: str | None = None
    cache_seconds: float = 60

    def __post_init__(self) -> None:
        if not self.users or not 0 <= self.cache_seconds <= 60:
            raise ValueError("A user group and cache duration between 0 and 60s are required")


class GroupAuthorizer:
    def __init__(self, directory: GroupDirectory, settings: GroupSettings) -> None:
        self.directory = directory
        self.settings = settings
        self.cache: dict[str, tuple[float, Role | None]] = {}
        self.lock = asyncio.Lock()

    async def principal(self, identity: Identity) -> Principal:
        async with self.lock:
            now = time.monotonic()
            cached = self.cache.get(identity.email)
            if cached and cached[0] > now:
                role = cached[1]
            else:
                # Check highest role first: admins/operators imply normal user capabilities.
                role = None
                groups = [
                    (self.settings.admins, Role.ADMIN),
                    (self.settings.operators, Role.OPERATOR),
                    (self.settings.users, Role.USER),
                ]
                for group, candidate in groups:
                    if group and await self.directory.has_member(group, identity.email):
                        role = candidate
                        break
                # Expiry starts before lookup; slow responses cannot extend authorization.
                self.cache[identity.email] = (now + self.settings.cache_seconds, role)
                self.cache = {key: value for key, value in self.cache.items() if value[0] > now}
        if role is None:
            raise PlatformError("FORBIDDEN", "Google account is not in an authorized group")
        return Principal(email=identity.email, subject=identity.subject, role=role)
