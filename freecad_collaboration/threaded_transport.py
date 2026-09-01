"""Run relay networking on a background asyncio thread."""

from __future__ import annotations

import asyncio
from queue import Empty, Queue
import threading

from .client import RelayClient


class ThreadedRelayTransport:
    """Thread boundary between Qt/FreeCAD and the asynchronous relay client."""

    def __init__(self, base_url: str, document_uid: str, client_id: str):
        self.client = RelayClient(base_url, document_uid, client_id)
        self.incoming = Queue()
        self.loop = None
        self.thread = None
        self.listener = None
        self.started = threading.Event()
        self.stopped = threading.Event()

    def start(self, *, register=None, download_checkpoint=False):
        if self.thread is not None:
            return
        self.thread = threading.Thread(
            target=self._thread_main,
            args=(register, download_checkpoint),
            name=f"FreeCADCollaboration-{self.client.client_id}",
            daemon=True,
        )
        self.thread.start()

    def _thread_main(self, register, download_checkpoint):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._connect(register, download_checkpoint))
            self.started.set()
            self.loop.run_forever()
        except Exception as exc:
            self.incoming.put(
                {"type": "transport_error", "error": type(exc).__name__, "message": str(exc)}
            )
            self.started.set()
        finally:
            try:
                self.loop.run_until_complete(self.client.close())
            except Exception:
                pass
            pending = asyncio.all_tasks(self.loop)
            for task in pending:
                task.cancel()
            if pending:
                self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.loop.close()
            self.stopped.set()

    async def _connect(self, register, download_checkpoint):
        if register is not None:
            name, state, environment_id, checkpoint = register
            await self.client.register_document(name, state, environment_id)
            await self.client.upload_checkpoint(checkpoint)
        if download_checkpoint:
            checkpoint = await self.client.download_checkpoint()
            self.incoming.put({"type": "checkpoint", "content": checkpoint})
        hello = await self.client.connect()
        self.incoming.put(hello)
        self.listener = asyncio.create_task(self._listen())

    async def _listen(self):
        try:
            while True:
                self.incoming.put(await self.client.receive())
        except Exception as exc:
            self.incoming.put(
                {"type": "transport_error", "error": type(exc).__name__, "message": str(exc)}
            )

    def _submit_coroutine(self, coroutine):
        if self.loop is None or self.stopped.is_set():
            raise RuntimeError("relay transport is not running")
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def completed(result):
            try:
                result.result()
            except Exception as exc:
                self.incoming.put(
                    {
                        "type": "transport_error",
                        "error": type(exc).__name__,
                        "message": str(exc),
                    }
                )

        future.add_done_callback(completed)
        return future

    def submit(self, packet, state):
        return self._submit_coroutine(self.client.submit(packet, state))

    def report_state(self, revision, state):
        return self._submit_coroutine(self.client.report_state(revision, state))

    def request_revisions(self, after, *, message_type="catch_up"):
        async def fetch():
            records = await self.client.revisions_after(after)
            self.incoming.put({"type": message_type, "records": records})

        return self._submit_coroutine(fetch())

    def acquire_locks(self, object_uids, ttl_seconds=120):
        return self._submit_coroutine(
            self.client.acquire_locks(object_uids, ttl_seconds=ttl_seconds)
        )

    def release_locks(self, object_uids):
        return self._submit_coroutine(self.client.release_locks(object_uids))

    def poll(self):
        try:
            return self.incoming.get_nowait()
        except Empty:
            return None

    def close(self, timeout=5):
        if self.thread is None:
            return
        if self.loop is not None and not self.stopped.is_set():
            try:
                future = asyncio.run_coroutine_threadsafe(self.client.close(), self.loop)
                future.result(timeout=timeout)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=timeout)
        self.thread = None
