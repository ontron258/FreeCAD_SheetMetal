"""Prove synchronization between two independent FreeCAD Python processes."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import FreeCAD as App

from freecad_collaboration import bootstrap_document
from freecad_collaboration.revision_store import RevisionStore
from freecad_collaboration.state import document_state


PEER = ROOT / "CollaborationTests" / "multiprocess_peer.py"
ENVIRONMENT_ID = "multiprocess-smoke"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for_server(url, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("collaboration relay did not start")


def wait_for_file(path: Path, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path.name}")


def peer_command(role, checkpoint, server, result, *, ready=None, length=88.0):
    command = [
        sys.executable,
        str(PEER),
        "--role",
        role,
        "--checkpoint",
        str(checkpoint),
        "--server",
        server,
        "--environment",
        ENVIRONMENT_ID,
        "--result",
        str(result),
        "--length",
        str(length),
    ]
    if ready:
        command.extend(("--ready", str(ready)))
    return command


def main():
    with tempfile.TemporaryDirectory(prefix="freecad-collaboration-") as temp_name:
        temp = Path(temp_name)
        checkpoint = temp / "shared.FCStd"
        database = temp / "revisions.sqlite3"
        receiver_ready = temp / "receiver.ready"
        receiver_result = temp / "receiver.json"
        sender_result = temp / "sender.json"

        document = App.newDocument("MultiProcessCheckpoint")
        box = document.addObject("Part::Box", "Box")
        box.Length = 10
        bootstrap_document(document)
        document.recompute()
        initial_state = document_state(document)
        document_uid = str(document.Uid)
        document.saveAs(str(checkpoint))
        App.closeDocument(document.Name)

        with RevisionStore(str(database)) as store:
            store.register_document(
                document_uid,
                "Multi-process checkpoint",
                initial_state,
                environment_id=ENVIRONMENT_ID,
            )

        port = free_port()
        server_url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "freecad_collaboration.server",
                "--database",
                str(database),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        receiver = None
        try:
            wait_for_server(server_url)
            receiver = subprocess.Popen(
                peer_command(
                    "receiver",
                    checkpoint,
                    server_url,
                    receiver_result,
                    ready=receiver_ready,
                ),
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wait_for_file(receiver_ready)
            sender = subprocess.run(
                peer_command(
                    "sender",
                    checkpoint,
                    server_url,
                    sender_result,
                    length=88.0,
                ),
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=45,
            )
            if sender.returncode:
                raise RuntimeError(f"sender failed:\n{sender.stdout}\n{sender.stderr}")
            receiver_output, _ = receiver.communicate(timeout=45)
            if receiver.returncode:
                raise RuntimeError(f"receiver failed:\n{receiver_output}")

            sender_data = json.loads(sender_result.read_text(encoding="utf-8"))
            receiver_data = json.loads(receiver_result.read_text(encoding="utf-8"))
            if sender_data["revision"] != 1 or receiver_data["revision"] != 1:
                raise AssertionError((sender_data, receiver_data))
            if sender_data["length"] != 88.0 or receiver_data["length"] != 88.0:
                raise AssertionError((sender_data, receiver_data))
            if sender_data["definition_hash"] != receiver_data["definition_hash"]:
                raise AssertionError("definition hashes differ")
            if sender_data["result_hash"] != receiver_data["result_hash"]:
                raise AssertionError("result hashes differ")
            print(json.dumps({"sender": sender_data, "receiver": receiver_data}, indent=2))
        finally:
            if receiver is not None and receiver.poll() is None:
                receiver.terminate()
                receiver.wait(timeout=5)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
