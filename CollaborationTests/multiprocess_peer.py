"""One process used by the collaboration multi-process smoke test."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import FreeCAD as App

from freecad_collaboration.session import DocumentSession
from freecad_collaboration.state import document_state


async def run(arguments):
    document = App.openDocument(arguments.checkpoint)
    session = DocumentSession(
        document,
        arguments.server,
        arguments.role,
        environment_id=arguments.environment,
    )
    try:
        await session.connect()
        if arguments.ready:
            Path(arguments.ready).write_text("ready", encoding="utf-8")

        if arguments.role == "sender":
            App.setActiveDocument(document.Name)
            document.openTransaction("Multi-process resize")
            document.getObject("Box").Length = arguments.length
            document.recompute()
            document.commitTransaction()
            await session.submit_next()
            message = await asyncio.wait_for(session.handle_next(), arguments.timeout)
        else:
            message = await asyncio.wait_for(session.handle_next(), arguments.timeout)

        state = document_state(document)
        result = {
            "role": arguments.role,
            "message_type": message.get("type"),
            "revision": session.revision,
            "length": float(document.getObject("Box").Length),
            "definition_hash": state.definition_hash,
            "result_hash": state.result_hash,
        }
        Path(arguments.result).write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        await session.close()
        App.closeDocument(document.Name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("sender", "receiver"), required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--environment", default="multiprocess-smoke")
    parser.add_argument("--result", required=True)
    parser.add_argument("--ready")
    parser.add_argument("--length", type=float, default=88.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
