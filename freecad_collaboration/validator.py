"""Headless FreeCAD worker that independently validates accepted revisions."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import uuid

from aiohttp import ClientSession
import FreeCAD as App

from .environment import default_environment_id
from .packet import TransactionPacket
from .replay import apply_packet
from .state import document_state


async def _post_validation(http, base_url, job, payload):
    async with http.post(
        f"{base_url}/documents/{job['document_uid']}/revisions/{job['revision']}/validation",
        json=payload,
    ) as response:
        response.raise_for_status()
        return await response.json()


async def validate_job(http, base_url, job, worker_id, environment_id):
    base_url = base_url.rstrip("/")
    payload = {"worker_id": worker_id, "environment_id": environment_id}
    document = None
    try:
        if job["environment_id"] != environment_id:
            raise RuntimeError(
                f"worker environment {environment_id!r} does not match "
                f"revision environment {job['environment_id']!r}"
            )
        async with http.get(
            f"{base_url}/documents/{job['document_uid']}/checkpoint"
        ) as response:
            response.raise_for_status()
            checkpoint = await response.read()
        async with http.get(
            f"{base_url}/documents/{job['document_uid']}/revisions", params={"after": 0}
        ) as response:
            response.raise_for_status()
            records = await response.json()

        document = App.newDocument(f"CollaborationValidation{uuid.uuid4().hex[:10]}")
        document.restoreContent(bytearray(checkpoint))
        document.recompute()
        reached_revision = 0
        for record in records:
            revision = int(record["revision"])
            if revision > int(job["revision"]):
                break
            if int(record["parent_revision"]) != reached_revision:
                raise RuntimeError("validation history is not contiguous")
            apply_packet(
                document,
                TransactionPacket.from_dict(record["packet"]),
                validate_document_uid=False,
            )
            reached_revision = revision
        if reached_revision != int(job["revision"]):
            raise RuntimeError(
                f"validation reached revision {reached_revision}, expected {job['revision']}"
            )
        payload["state"] = document_state(document).to_dict()
    except Exception as exc:
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if document is not None:
            App.closeDocument(document.Name)
    return await _post_validation(http, base_url, job, payload)


async def validate_pending(base_url, worker_id, environment_id, *, limit=10):
    base_url = base_url.rstrip("/")
    async with ClientSession() as http:
        async with http.get(
            f"{base_url}/validation/jobs", params={"limit": int(limit)}
        ) as response:
            response.raise_for_status()
            jobs = await response.json()
        reports = []
        for job in jobs:
            reports.append(
                await validate_job(http, base_url, job, worker_id, environment_id)
            )
        return reports


async def run_worker(base_url, worker_id, environment_id, *, once=False, interval=1):
    while True:
        reports = await validate_pending(base_url, worker_id, environment_id)
        for report in reports:
            status = "valid" if report["valid"] else "INVALID"
            print(
                f"{report['document_uid']}@{report['revision']} {status}",
                flush=True,
            )
        if once:
            return 0 if all(report["valid"] for report in reports) else 1
        await asyncio.sleep(max(0.1, float(interval)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--environment", default=default_environment_id())
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1)
    arguments = parser.parse_args(argv)
    raise SystemExit(
        asyncio.run(
            run_worker(
                arguments.server,
                arguments.worker_id,
                arguments.environment,
                once=arguments.once,
                interval=arguments.interval,
            )
        )
    )


if __name__ == "__main__":
    main()
