"""Operator SSH transport: public WSS to a registered cluster's private TCP port 22.

SSH authentication and host-key verification remain between OpenSSH and the Mac.
Tickets are single-use, process-local, and bound to a revocable human session.
"""

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fl_common.errors import PlatformError
from fl_common.models.base import utcnow
from fl_common.models.scheduler import Principal

from .auth.api import AuthAPI
from .auth.sessions import digest
from .db.async_calls import database_call
from .db.core import Database
from .db.models import Cluster, Event
from .network.headscale import tailnet_address

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
BLOCKED_STATES = {"DESTROYED", "POWERED_OFF", "FORCE_POWER_OFF_PENDING"}
CHUNK_SIZE = 65536


@dataclass
class Ticket:
    session_id: UUID
    cluster_id: UUID
    human_session: UUID
    principal: Principal
    address: str
    token_hash: str
    expires_at: datetime
    used: bool = False


class SshRelay:
    def __init__(self, db: Database, auth: AuthAPI) -> None:
        self.db, self.auth = db, auth
        self.tickets: dict[UUID, Ticket] = {}
        self.handlers: set[asyncio.Task[Any]] = set()
        self.connector: Connector = asyncio.open_connection
        self.check_interval = 30.0
        self.max_duration = 8 * 3600.0
        self.router = APIRouter()
        self.router.add_api_route("/api/clusters/{cluster_id}/ssh", self.issue, methods=["POST"])
        self.router.add_api_websocket_route("/api/ssh/{session_id}", self.connect)

    def address(self, cluster_id: UUID) -> str:
        with self.db.transaction() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is None:
                raise PlatformError("CLUSTER_NOT_FOUND", "Cluster does not exist")
            if cluster.state in BLOCKED_STATES or cluster.desired_state in BLOCKED_STATES:
                raise PlatformError("SSH_UNAVAILABLE", "Cluster is retired or powered off")
            # Only trusted Headscale enrollment metadata can choose the destination.
            for address in cluster.headscale_addresses or []:
                try:
                    if tailnet_address(address):
                        return address
                except ValueError:
                    continue
        raise PlatformError("SSH_UNAVAILABLE", "Cluster has no enrolled Headscale address")

    @staticmethod
    def require_operator(principal: Principal) -> None:
        if not principal.permits("cluster:ssh"):
            raise PlatformError("FORBIDDEN", "SSH access requires an operator or administrator")

    async def issue(self, cluster_id: UUID, request: Request) -> dict[str, str]:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise PlatformError("UNAUTHENTICATED", "SSH requires a terminal login")
        human_session, principal = await self.auth.sessions.identify(token)
        self.require_operator(principal)
        address = await database_call(self.address, cluster_id)
        now = utcnow()
        self.tickets = {
            key: item for key, item in self.tickets.items() if item.used or item.expires_at > now
        }
        tickets = list(self.tickets.values())
        if (
            len(tickets) >= 32
            or sum(item.principal.subject == principal.subject for item in tickets) >= 2
            or sum(item.cluster_id == cluster_id for item in tickets) >= 4
        ):
            raise PlatformError("SLOW_DOWN", "SSH connection limit reached; close a session first")
        capability = secrets.token_urlsafe(32)
        ticket = Ticket(
            uuid4(),
            cluster_id,
            human_session,
            principal,
            address,
            digest(capability),
            now + timedelta(seconds=60),
        )
        self.tickets[ticket.session_id] = ticket
        return {
            "session_id": str(ticket.session_id),
            "token": capability,
            "expires_at": ticket.expires_at.isoformat(),
        }

    async def authorize(self, ticket: Ticket) -> None:
        principal = await self.auth.sessions.principal_for_session(ticket.human_session)
        self.require_operator(principal)
        if await database_call(self.address, ticket.cluster_id) != ticket.address:
            raise PlatformError("SSH_UNAVAILABLE", "Cluster network identity changed")

    def audit(self, ticket: Ticket, event: str, reason: str) -> None:
        with self.db.transaction() as session:
            session.add(
                Event(
                    cluster_id=ticket.cluster_id,
                    type=event,
                    payload={
                        "session_id": str(ticket.session_id),
                        "actor": ticket.principal.email,
                        "subject": ticket.principal.subject,
                        "reason": reason,
                    },
                )
            )

    async def connect(self, websocket: WebSocket, session_id: UUID) -> None:
        ticket = self.tickets.get(session_id)
        scheme, _, token = websocket.headers.get("authorization", "").partition(" ")
        if (
            websocket.headers.get("origin")
            or scheme.lower() != "bearer"
            or not 32 <= len(token) <= 256
            or ticket is None
            or ticket.used
            or ticket.expires_at <= utcnow()
            or not secrets.compare_digest(ticket.token_hash, digest(token))
        ):
            await websocket.close(code=1008)
            return
        # Consume before yielding: simultaneous handshakes cannot replay the capability.
        ticket.used = True
        handler = asyncio.current_task()
        assert handler is not None
        self.handlers.add(handler)
        writer = None
        tasks: list[asyncio.Task[None]] = []
        opened = False
        reason = "SSH_CLOSED"
        try:
            await self.authorize(ticket)
            await websocket.accept()
            try:
                async with asyncio.timeout(10):
                    reader, writer = await self.connector(ticket.address, 22)
            except (OSError, TimeoutError) as error:
                raise PlatformError(
                    "SSH_UNREACHABLE", "Cannot reach cluster SSH on port 22; check Remote Login"
                ) from error
            await database_call(self.audit, ticket, "SSH_OPENED", "CONNECTED")
            opened = True

            async def upstream() -> None:
                assert writer is not None
                while True:
                    data = await websocket.receive_bytes()
                    if not data or len(data) > CHUNK_SIZE:
                        raise PlatformError("SSH_PROTOCOL", "Invalid SSH transport frame")
                    writer.write(data)
                    await writer.drain()

            async def downstream() -> None:
                while data := await reader.read(CHUNK_SIZE):
                    await websocket.send_bytes(data)

            async def monitor() -> None:
                while True:
                    await asyncio.sleep(self.check_interval)
                    await self.authorize(ticket)

            tasks = [asyncio.create_task(call()) for call in (upstream, downstream, monitor)]
            async with asyncio.timeout(self.max_duration):
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        except PlatformError as error:
            reason = error.code
            if websocket.application_state.name == "CONNECTED":
                try:
                    await websocket.send_json({"code": error.code, "message": error.message})
                except (OSError, RuntimeError, WebSocketDisconnect):
                    pass
        except TimeoutError:
            reason = "SSH_SESSION_EXPIRED"
        except asyncio.CancelledError:
            reason = "SSH_SHUTDOWN"
            raise
        except (OSError, RuntimeError, WebSocketDisconnect, KeyError):
            reason = "SSH_DISCONNECTED"
        finally:
            try:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if writer is not None:
                    writer.close()
                    try:
                        async with asyncio.timeout(5):
                            await writer.wait_closed()
                    except (OSError, TimeoutError):
                        pass
            finally:
                try:
                    # Record closure before sending the close frame. A client/context can
                    # cancel its handler immediately after receiving that frame.
                    await database_call(
                        self.audit, ticket, "SSH_CLOSED" if opened else "SSH_DENIED", reason
                    )
                finally:
                    self.tickets.pop(session_id, None)
                    try:
                        await websocket.close(code=1000 if reason == "SSH_CLOSED" else 1008)
                    except (OSError, RuntimeError, WebSocketDisconnect):
                        pass
                    finally:
                        self.handlers.discard(handler)

    async def close(self) -> None:
        handlers = list(self.handlers)
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        self.tickets.clear()
