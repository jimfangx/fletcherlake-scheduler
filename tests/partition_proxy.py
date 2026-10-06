"""TCP connection loss without stopping the scheduler or modifying agent code."""

import asyncio
from contextlib import suppress


class FaultProxy:
    def __init__(self, destination_port):
        self.destination_port = destination_port
        self.enabled = True
        self.tasks = set()
        self.writers = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        outputs = [writer]
        self.writers.add(writer)
        copies = []
        try:
            if not self.enabled:
                return
            remote_reader, remote_writer = await asyncio.open_connection(
                "127.0.0.1", self.destination_port
            )
            outputs.append(remote_writer)
            self.writers.add(remote_writer)
            copies = [
                asyncio.create_task(self.copy(reader, remote_writer)),
                asyncio.create_task(self.copy(remote_reader, writer)),
            ]
            done, _ = await asyncio.wait(copies, return_when=asyncio.FIRST_COMPLETED)
            for copy in done:
                await copy
        except (ConnectionError, OSError):
            pass
        finally:
            for copy in copies:
                copy.cancel()
            await asyncio.gather(*copies, return_exceptions=True)
            for output in outputs:
                output.close()
                self.writers.discard(output)
                with suppress(ConnectionError, OSError):
                    await output.wait_closed()
            self.tasks.discard(task)

    @staticmethod
    async def copy(reader, writer):
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()

    async def partition(self):
        self.enabled = False
        for writer in list(self.writers):
            writer.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def restore(self):
        self.enabled = True

    async def stop(self):
        self.server.close()
        await self.partition()
        await self.server.wait_closed()
