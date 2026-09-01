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
- Collapses FreeCAD's standard `Edit Object.Property` transactions to the one
  editable input property. Derived shapes, caches, pattern children, and other
  recompute output are regenerated independently by each client instead of
  being serialized as hundreds of redundant operations.
- Suppresses FreeCAD's follow-up `Sketch recompute` transactions after the
  user-owned sketch geometry/constraint edit has already been captured. This
  prevents dependency-graph fan-out from becoming dozens of network edits.
- Uses FreeCAD's native `dumpContent`, `dumpPropertyContent`, `restoreContent`,
  and `restorePropertyContent` APIs for workbench-neutral payloads.
- Encodes native persistence payloads as Base64 so packets can be stored as
  JSON during the prototype.
- Replays packets inside one FreeCAD transaction and can suppress a target
  recorder to prevent network echo.
- Treats protected generated properties and already-removed dependent objects
  as local recompute results during replay.
- Computes separate canonical hashes for the parametric definition and the
  generated geometry. Definition payloads exclude native ZIP timestamps;
  Part shapes use sorted topology, mass properties, bounds, and curve/surface
  samples instead of process-unstable native BRep bytes.
- Normalizes save/reload-safe quantities, matrices, sketch geometry, null and
  aggregate shapes, generated labels, and recompute caches so a complex model
  compares identically after a normal FCStd reopen.
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
- Replays `App::PropertyExpressionEngine` semantically with `setExpression`.
  FreeCAD's native `restorePropertyContent` accepts this property without an
  error but does not restore sketch constraint expressions, which previously
  left a peer with the literal constraint value instead of its configuration
  expression.
- Provides an asynchronous client and cooperative FreeCAD document session
  that submit local transactions, apply remote transactions without echo, and
  catch a late client up from the revision log.
- Runs relay networking on a background asyncio thread while a Qt timer applies
  incoming packets on FreeCAD's main GUI thread.
- Uses a local-first interactive submission path: a committed GUI transaction
  is queued immediately without synchronously hashing every object and shape.
  The relay sequences it provisionally and a later headless pass supplies the
  authoritative definition and generated-result hashes.
- Synchronizes edits bidirectionally between two independent FreeCAD Python
  processes through the real relay protocol.
- Stores an immutable, complete FCStd revision-zero checkpoint so an empty GUI
  document can join late through FreeCAD's normal load path and replay the
  remaining log. Legacy in-memory persistence checkpoints remain readable.
- Provides cooperative UUID-addressed feature locks with expiry, renewal, and
  disconnect cleanup. Packets touching an object locked by another client are
  rejected before sequencing.
- Registers a dockable GUI panel with share, join, disconnect, lock-selection,
  status, revision, document UUID, and validation/error reporting controls.
- Automatically rebases simultaneous disjoint edits: unaccepted local
  transactions are undone, missing revisions are applied, and local packets
  are replayed and resubmitted. Same-address edits enter an explicit conflict
  state and retain the local result until the user discards it.
- Generates a canonical environment lockfile from the exact FreeCAD build,
  Python and protocol versions, addon Git revision, and a path-independent
  digest of relevant addon code. A deployment may override its identity with
  `FREECAD_COLLABORATION_ENVIRONMENT_ID`.
- Provides a separate headless FreeCAD worker that reconstructs accepted
  revisions from checkpoint plus packets and independently validates both
  definition and generated-geometry hashes. Results are persisted and
  broadcast to GUI clients.

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
GET  /validation/jobs?limit={count}
GET  /documents/{document_uid}/revisions/{revision}/validation
POST /documents/{document_uid}/revisions/{revision}/validation
GET  /documents/{document_uid}/ws?client_id={client_id}
```

The WebSocket currently accepts `submit`, `state_report`, `acquire_locks`, and
`release_locks` messages. Batch/headless clients may still submit state hashes,
but interactive GUI clients omit them so full model hashing never blocks the
editing loop. Those revisions have empty provisional hashes until the
independent validator reconstructs the revision, supplies the authoritative
hashes, and marks it valid or invalid.

`freecad_collaboration.session.DocumentSession` now connects the transaction
recorder and replayer to this protocol. Its network methods are asynchronous,
while packet application is deliberately explicit so the eventual GUI addon
can marshal document changes onto FreeCAD's Qt main thread.

`freecad_collaboration.qt_session.QtDocumentSession` is that first GUI-thread
adapter. `ThreadedRelayTransport` owns the asyncio event loop and WebSocket on
a daemon thread; a `QTimer` drains immutable messages and performs every
FreeCAD document mutation on the Qt thread. Current session states are
`disconnected`, `connecting`, `connected`, `submitting`, `catching_up`,
`rebasing`, `conflict`, `validating`, `synchronized`, and `error`.

Run a validator continuously beside the relay:

```powershell
& $freecadPython -m freecad_collaboration.validator `
    --server http://127.0.0.1:8765 `
    --worker-id local-validator
```

Use `--once` for CI or smoke tests. Its environment lock identity must match
the document's identity.

## GUI prototype

On this feature branch the Sheet Metal workbench exposes **Sheet Metal >
Collaboration > Collaboration panel**. This is only a convenient loading point;
the collaboration package does not depend on SheetMetal features.

To start a local session:

1. Run the relay command above.
2. Open the model and choose **Share current**. The server stores its canonical
   state and a complete native FCStd revision-zero checkpoint without replacing
   the source file.
3. On another FreeCAD client, create an empty document, enter the same document
   UUID, and choose **Join**. The client replaces that empty document with the
   downloaded FCStd checkpoint and catches up through the transaction log.
4. Before editing a shared feature, select it in the tree or 3D view and choose
   **Lock selection**. Locks are renewed every minute and removed on disconnect.
5. Watch **Headless validation** for the independent result. If simultaneous
   work overlaps the same object property, inspect the retained local result
   and choose **Discard conflicting local edits** to return to server state.

Use **Save environment lockfile** to write the complete JSON manifest. The same
operation is available on the command line:

```powershell
& $freecadPython -m freecad_collaboration.environment `
    --output collaboration-environment.lock.json
```

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
- The GUI outbox is currently in memory. Edits continue locally while uploads
  are pending, but a crash or closing FreeCAD before acknowledgement can lose
  queued packets. A durable local SQLite outbox and reconnect/resume protocol
  are the next reliability slice.
- Compact property-edit recording currently recognizes FreeCAD's standard
  `Edit Object.Property` transaction name. Other commands use the generic
  filtered transaction fallback until operation intent is represented
  explicitly by command/workbench integrations.
- Labels and known generated drawing names are synchronized by packets but are
  excluded from canonical state hashes because FreeCAD/Python features may
  renumber them during an otherwise equivalent save/reopen.
- Face/edge subelement references still rely on FreeCAD's topological naming.
- View-provider state is intentionally not recorded yet.
- Locks are cooperative and object-granular. Automatic locking when a task panel
  starts editing and property-level locks are not implemented yet.
- Automatic rebase covers address-disjoint transactions. Semantically
  mergeable edits to the same list-valued property still require a future
  workbench-aware merge layer. Two dimensions in one sketch therefore still
  collide at the coarse `Sketch.Constraints`/`Sketch.Geometry` property level.
- A submission rejected because another client already holds a lock leaves the
  local client divergent; the current recovery is to disconnect and join into
  a new empty document. A one-click reset/rebase workflow remains.
- Headless validation is asynchronous and currently requires a separately
  started worker. Its current reconstruction starts from revision zero for each
  validation job, so it should be run after an idle/debounce period for large
  models until incremental checkpoint reuse is implemented. Invalid revisions
  are flagged but not automatically rolled back or quarantined.
- The relay has no authentication or network hardening yet.
- Object creation replay assumes the receiving FreeCAD installation provides
  the same registered object types and Python proxies.
- This worktree starts at committed revision `6c5fcff`; unrelated uncommitted
  changes from the main SheetMetal checkout are intentionally absent.

## Verification

Run with the bundled FreeCAD Python interpreter:

```powershell
$freecadPython = 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe'
& $freecadPython -m unittest discover -v -s CollaborationTests
```

Run the separate-process smoke test:

```powershell
& $freecadPython tools\run_collaboration_multiprocess_smoke.py
& $freecadPython tools\run_collaboration_two_gui_smoke.py
```

The second command launches two independent hidden `freecad.exe` GUI processes,
has one upload the checkpoint, has the other restore it, synchronizes an edit,
compares both client hashes, launches a separate headless validator, and
requires its independently reconstructed revision to match.

The current suite contains 34 tests. A manual persistent client launcher for
local two-window trials is available at `tools/live_collaboration_client.py`;
it reads the documented `FREECAD_COLLAB_LIVE_*` environment variables and is
passed to `FreeCAD.exe` as a positional startup script. The launcher preloads
the model's installed SheetMetal Python feature proxies before opening or
restoring the FCStd checkpoint, satisfying FreeCAD's safe-unpickling policy on
both the sharing and joining clients.

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
   **Object-level cooperative locks and automatic address-disjoint rebase are
   implemented; task-panel integration and semantic list merging remain.**
6. Add authentication, authorization, TLS termination, packet-size limits,
   schema validation, and database lifecycle management before non-local use.
7. Add an independent headless validator. **Implemented as a polling worker;
   automatic worker supervision and invalid-revision quarantine remain.**
8. Persist the GUI packet outbox and resume it across disconnect/restart, then
   add idle/debounced incremental validator checkpoints so validation never
   competes with interactive recompute.
