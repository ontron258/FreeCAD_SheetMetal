"""Launch two hidden FreeCAD GUI clients and synchronize one edit."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
PEER = ROOT / "CollaborationTests" / "gui_process_peer.py"
ENVIRONMENT_ID = "two-gui-smoke"
DOCUMENT_UID = "a362b447-6ee1-4e54-b243-8c37ef7ce351"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(path, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for {path.name}")


def wait_for_server(url, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{url}/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.05)
    raise TimeoutError("collaboration relay did not start")


def hidden_startupinfo():
    if os.name != "nt":
        return None
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    return startup


def launch_peer(freecad, config_path):
    environment = os.environ.copy()
    environment["FREECAD_COLLABORATION_GUI_PEER_CONFIG"] = str(config_path)
    return subprocess.Popen(
        [str(freecad), str(PEER)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        startupinfo=hidden_startupinfo(),
    )


def main():
    freecad = Path(sys.executable).with_name("freecad.exe")
    if not freecad.exists():
        raise RuntimeError(f"FreeCAD GUI executable not found beside {sys.executable}")
    with tempfile.TemporaryDirectory(prefix="freecad-two-gui-") as temp_name:
        temp = Path(temp_name)
        port = free_port()
        server_url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "freecad_collaboration.server",
                "--database",
                str(temp / "relay.sqlite3"),
                "--port",
                str(port),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            startupinfo=hidden_startupinfo(),
        )
        peers = []
        try:
            wait_for_server(server_url)
            trigger = temp / "edit.trigger"
            configs = {}
            for role in ("sender", "receiver"):
                configs[role] = {
                    "role": role,
                    "server": server_url,
                    "environment": ENVIRONMENT_ID,
                    "document_uid": DOCUMENT_UID,
                    "ready": str(temp / f"{role}.ready"),
                    "trigger": str(trigger),
                    "result": str(temp / f"{role}.json"),
                }
                (temp / f"{role}-config.json").write_text(
                    json.dumps(configs[role]), encoding="utf-8"
                )

            sender = launch_peer(freecad, temp / "sender-config.json")
            peers.append(sender)
            wait_for(Path(configs["sender"]["ready"]))
            receiver = launch_peer(freecad, temp / "receiver-config.json")
            peers.append(receiver)
            wait_for(Path(configs["receiver"]["ready"]))
            trigger.write_text("edit", encoding="utf-8")
            wait_for(Path(configs["sender"]["result"]))
            wait_for(Path(configs["receiver"]["result"]))

            validator = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "freecad_collaboration.validator",
                    "--server",
                    server_url,
                    "--worker-id",
                    "two-gui-smoke-validator",
                    "--environment",
                    ENVIRONMENT_ID,
                    "--once",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
                startupinfo=hidden_startupinfo(),
            )
            if validator.returncode:
                validation_details = ""
                try:
                    with urlopen(
                        f"{server_url}/documents/{DOCUMENT_UID}/revisions/1/validation",
                        timeout=5,
                    ) as response:
                        validation_details = response.read().decode("utf-8")
                except Exception as exc:
                    validation_details = f"unable to read validation result: {exc}"
                raise RuntimeError(
                    f"headless validator failed:\n{validator.stdout}\n{validator.stderr}\n"
                    f"{validation_details}"
                )
            with urlopen(
                f"{server_url}/documents/{DOCUMENT_UID}/revisions/1/validation",
                timeout=5,
            ) as response:
                validation = json.loads(response.read().decode("utf-8"))
            if not validation["valid"]:
                raise AssertionError(validation)

            for peer in peers:
                output, _ = peer.communicate(timeout=15)
                if peer.returncode:
                    raise RuntimeError(f"GUI peer failed:\n{output}")
            results = {
                role: json.loads(Path(configs[role]["result"]).read_text(encoding="utf-8"))
                for role in ("sender", "receiver")
            }
            if not all(result.get("ok") for result in results.values()):
                raise AssertionError(results)
            if any(result["revision"] != 1 or result["length"] != 91 for result in results.values()):
                raise AssertionError(results)
            if results["sender"]["definition_hash"] != results["receiver"]["definition_hash"]:
                raise AssertionError("GUI peers have different definition hashes")
            if results["sender"]["result_hash"] != results["receiver"]["result_hash"]:
                raise AssertionError("GUI peers have different result hashes")
            print(json.dumps({"peers": results, "validation": validation}, indent=2))
        finally:
            for peer in peers:
                if peer.poll() is None:
                    peer.terminate()
                    peer.wait(timeout=5)
                if peer.stdout is not None:
                    peer.stdout.close()
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server.stdout is not None:
                server.stdout.close()


if __name__ == "__main__":
    main()
