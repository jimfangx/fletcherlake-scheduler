"""Partition/restoration and shutdown with active TCP streams must finish without leaks."""

import asyncio

from tests.partition_proxy import FaultProxy


async def test_proxy_drops_restores_and_closes_active_connections():
    async def echo(reader, writer):
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    proxy = await FaultProxy(server.sockets[0].getsockname()[1]).start()
    writers = []
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writers.append(writer)
        writer.write(b"before\x00partition")
        assert await asyncio.wait_for(reader.readexactly(16), 2) == b"before\x00partition"
        await proxy.partition()
        assert await asyncio.wait_for(reader.read(), 2) == b""
        blocked, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writers.append(writer)
        assert await asyncio.wait_for(blocked.read(), 2) == b""
        proxy.restore()
        restored, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writers.append(writer)
        writer.write(b"restored")
        assert await asyncio.wait_for(restored.readexactly(8), 2) == b"restored"
        # Python 3.12 Server.wait_closed waits for connections as well as the listener.
        await asyncio.wait_for(proxy.stop(), 2)
        assert await asyncio.wait_for(restored.read(), 2) == b""
        assert not proxy.tasks and not proxy.writers and server.is_serving()
    finally:
        await asyncio.wait_for(proxy.stop(), 2)
        for writer in writers:
            writer.close()
            await writer.wait_closed()
        server.close()
        await asyncio.wait_for(server.wait_closed(), 2)
