"""Publish a local document into a separate, immutable server session."""
import base64
import json
import urllib.request
import uuid

from .checkpoint import native_checkpoint_bytes
from .identity import bootstrap_document
from .state import document_state


def publish_snapshot(document, base_url, environment_id):
    bootstrap_document(document)
    document.recompute()
    document_uid = str(uuid.uuid4())
    payload = {
        "document_uid": document_uid,
        "name": document.Label,
        "environment_id": environment_id,
        "state": document_state(document).to_dict(),
        "checkpoint": base64.b64encode(native_checkpoint_bytes(document)).decode("ascii"),
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/snapshots",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        head = json.load(response)
    if head["document_uid"] != document_uid or not head["has_checkpoint"]:
        raise RuntimeError("server did not acknowledge the new checkpoint")
    return head
