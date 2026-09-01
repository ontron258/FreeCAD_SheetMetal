# FreeCAD Transaction Collaboration Prototype

## Objective

Prove that ordinary FreeCAD transactions can be represented by a generic,
versioned packet and replayed through FreeCAD's public document API. The same
Python core is intended to run in a future GUI addon and a headless validator.

This prototype is deliberately separate from the SheetMetal feature code. A
SheetMetal-specific semantic layer may be added later, but generic persistence
packets must remain the fallback for unknown workbenches.

## Current vertical slice

- Adds a hidden `App::PropertyUUID` named `CollaborationUid` to document
  objects.
- Seeds an `App::Part` identity from its existing `Uid` when possible.
- Repairs UUIDs duplicated by copying an object.
- Observes FreeCAD transaction open, commit, and abort callbacks.
- Records object creation/deletion, persistent property changes, and dynamic
  property addition/removal.
- Uses FreeCAD's native `dumpContent`, `dumpPropertyContent`, `restoreContent`,
  and `restorePropertyContent` APIs for workbench-neutral payloads.
- Encodes native persistence payloads as Base64 so packets can be stored as
  JSON during the prototype.
- Replays packets inside one FreeCAD transaction and can suppress a target
  recorder to prevent network echo.
- Computes separate canonical hashes for the parametric definition and the
  generated geometry. Native ZIP timestamps are excluded from the hash.
- Stores accepted packets in an append-only SQLite revision log.
- Rejects stale base revisions and incompatible environment IDs.
- Treats a repeated transaction UUID as an idempotent retry.
- Records whether each client's definition and result hashes match the
  accepted revision.
- Provides a localhost HTTP/WebSocket relay that broadcasts accepted packets
  to all clients connected to the document.
- Encodes same-document link, link-list, link-subelement, and
  link-subelement-list targets by collaboration UUID instead of relying on
  matching FreeCAD internal names.
- Provides an asynchronous client and cooperative FreeCAD document session
  that submit local transactions, apply remote transactions without echo, and
  catch a late client up from the revision log.
- Runs relay networking on a background asyncio thread while a Qt timer applies
  incoming packets on FreeCAD's main GUI thread.
- Synchronizes edits bidirectionally between two independent FreeCAD Python
  processes through the real relay protocol.
- Stores an immutable native FreeCAD revision-zero checkpoint so an empty
  document can join late, restore the model, and replay the remaining log.
- Provides cooperative UUID-addressed feature locks with expiry, renewal, and
  disconnect cleanup. Packets touching an object locked by another client are
  rejected before sequencing.
- Registers a dockable GUI panel with share, join, disconnect, lock-selection,
  status, revision, document UUID, and validation/error reporting controls.
- Generates a default compatibility identity from the exact FreeCAD build,
  Python version, packet schema, and collaboration-core version. A deployment
  can override it with `FREECAD_COLLABORATION_ENVIRONMENT_ID` while an addon
  lockfile format is developed.

## Local relay

The prototype relay uses `aiohttp`, which is included in the current bundled
FreeCAD Python environment:

```powershell
$freecadPython = 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe'
& $freecadPython -m freecad_collaboration.server `
    --database C:\FreeCAD\tmp\freecad-collaboration.sqlite3 `
    --host 127.0.0.1 `
    --port 8765
```

Current endpoints:

```text
GET  /health
POST /documents
GET  /documents/{document_uid}/head
PUT  /documents/{document_uid}/checkpoint
GET  /documents/{document_uid}/checkpoint
GET  /documents/{document_uid}/revisions?after={revision}
GET  /documents/{document_uid}/ws?client_id={client_id}
```

The WebSocket currently accepts `submit`, `state_report`, `acquire_locks`, and
`release_locks` messages. It is a sequencer rather than an authoritative CAD
worker: the submitting client still supplies the resulting state hashes.

`freecad_collaboration.session.DocumentSession` now connects the transaction
recorder and replayer to this protocol. Its network methods are asynchronous,
while packet application is deliberately explicit so the eventual GUI addon
can marshal document changes onto FreeCAD's Qt main thread.

`freecad_collaboration.qt_session.QtDocumentSession` is that first GUI-thread
adapter. `ThreadedRelayTransport` owns the asyncio event loop and WebSocket on
a daemon thread; a `QTimer` drains immutable messages and performs every
FreeCAD document mutation on the Qt thread. Current session states are
`disconnected`, `connecting`, `connected`, `submitting`, `catching_up`,
`validating`, `synchronized`, and `error`.

## GUI prototype

On this feature branch the Sheet Metal workbench exposes **Sheet Metal >
Collaboration > Collaboration panel**. This is only a convenient loading point;
the collaboration package does not depend on SheetMetal features.

To start a local session:

1. Run the relay command above.
2. Open the model and choose **Share current**. The server stores its canonical
   state and native revision-zero checkpoint.
3. On another FreeCAD client, create an empty document, enter the same document
   UUID, and choose **Join**. The client downloads the checkpoint and catches up
   through the transaction log.
4. Before editing a shared feature, select it in the tree or 3D view and choose
   **Lock selection**. Locks are renewed every minute and removed on disconnect.

Joining a non-empty document remains supported when it is already an identical
copy of revision zero; no checkpoint is downloaded in that case.

## Packet addressing

Packets address a mutation using:

```text
Document.Uid
  + CollaborationUid
  + property name
```

The FreeCAD internal object `Name` is included as bootstrap and diagnostic
metadata. It is not intended to remain the authoritative collaboration
identity.

## Important current limitations

- UUID link encoding currently covers the common link property families.
  Cross-document links require a document resolver and have not yet been
  exercised end to end.
- Full object restore still contains native name-addressed payloads, followed
  by UUID link rebinding. This works in the covered tests but needs broader
  workbench coverage.
- Geometry and Sketcher list elements do not yet receive collaboration-level
  identifiers.
- Face/edge subelement references still rely on FreeCAD's topological naming.
- View-provider state is intentionally not recorded yet.
- Locks are cooperative and object-granular. Automatic locking when a task panel
  starts editing, property-level locks, and an automatic rebase workflow are not
  implemented yet.
- A submission rejected because another client already holds a lock leaves the
  local client divergent; the current recovery is to disconnect and join into
  a new empty document. A one-click reset/rebase workflow remains.
- The environment ID is currently supplied by the caller; generation from a
  FreeCAD/addon Git lockfile has not been implemented.
- The relay trusts the submitting client's resulting hashes and has no
  authentication or network hardening yet.
- Object creation replay assumes the receiving FreeCAD installation provides
  the same registered object types and Python proxies.
- This worktree starts at committed revision `6c5fcff`; unrelated uncommitted
  changes from the main SheetMetal checkout are intentionally absent.

## Verification

Run with the bundled FreeCAD Python interpreter:

```powershell
$freecadPython = 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe'
& $freecadPython -m unittest `
    CollaborationTests.test_transactions `
    CollaborationTests.test_revision_store `
    CollaborationTests.test_relay `
    CollaborationTests.test_session `
    CollaborationTests.test_qt_session
```

Run the separate-process smoke test:

```powershell
& $freecadPython tools\run_collaboration_multiprocess_smoke.py
& $freecadPython tools\run_collaboration_two_gui_smoke.py
```

The second command launches two independent hidden `freecad.exe` GUI processes,
has one upload the checkpoint, has the other restore it, synchronizes an edit,
and compares both definition and result hashes.

## Next slice

1. Add a canonical document/object state hash that excludes GUI and cached
   shape noise. **Implemented for the current property policy; expand tests.**
2. Convert native link payload names to UUID-addressed link operations.
   **Implemented for common same-document link types; expand cross-document
   coverage.**
3. Add an append-only local SQLite revision store and packet sequencer.
   **Implemented.**
4. Connect two GUI instances through the localhost WebSocket relay.
   **Implemented, including the dock panel and two-process GUI smoke test.**
5. Add conflict detection beyond base revision, at the property address level.
   **Object-level cooperative locks are implemented; task-panel integration,
   property granularity, and rebase remain.**
6. Add authentication, authorization, TLS termination, packet-size limits,
   schema validation, and database lifecycle management before non-local use.
7. Evaluate an on-demand headless validator after client-to-client replay is
   reliable.
