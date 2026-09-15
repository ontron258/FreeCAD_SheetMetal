# Repository Guide for Agents

## Purpose and source of truth

This repository is the source of truth for the FreeCAD SheetMetal workbench.
Do not edit the installed FreeCAD addon copy and then try to reconstruct the
change here. Make changes in this repository, test them, and deploy the changed
files to FreeCAD only as a verification step.

For the current Windows development environment:

- Repository: `C:\FreeCAD\FreeCAD_SheetMetal`
- FreeCAD installation: `C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311`
- Installed addon junction: `C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\Mod\SheetMetal`
- Example test document: `C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\data\examples\Sheetmetal test.FCStd`

Treat the installed addon as a deployment target. Never commit files from the
installed addon directory.
FreeCAD loads the repository directly through this junction. The former standalone
user addon was moved into `C:\FreeCAD\backups\sheetmetal-installed-copy-before-main-*`.
Do not recreate a competing user addon copy; verify imported paths in a fresh process.

## Usage documentation and repository boundary

Read `USAGE.md` for fork installation, the currently configured branch, local
paths and how this addon works alongside the independent Collaboration addon.
`README.md` links that guide and preserves the upstream modeling tutorials.
Keep these usage notes current when changing installation or user workflows.

Modeling/material/unfold/DXF changes belong here. General synchronization,
server storage/protocol, authentication, validation and collaboration GUI changes
belong in `C:\FreeCAD\FreeCAD_Collaboration`, whose README documents its usage.
SheetMetal must remain usable without a collaboration server. Do not restore
the removed embedded collaboration package or SheetMetal collaboration submenu.
When a task spans both repos, inspect/test each and commit/push each separately.

## Release Manager boundary

General drawing naming and derivative lineage now live in the independent
`C:\FreeCAD\FreeCAD_ReleaseManager` addon. `SheetMetalNaming.py` is an optional
compatibility shim; preserve its public entry points and saved property names.
The mirror command delegates metadata creation while retaining all geometry behavior.
Do not reintroduce release records, naming observers or packaging into SheetMetal.
Material configuration and the VarSet propagation repair remain modeling responsibilities.
Install Release Manager when running naming/lineage regression tests, and verify basic
modeling still works without it. Follow its AGENTS.md when changing the shared contracts.

## Git workflow

The remotes have distinct roles:

- `origin`: `ontron258/FreeCAD_SheetMetal`, the development fork.
- `upstream`: `shaise/FreeCAD_SheetMetal`, the official project.

Branch conventions:

- `master` mirrors `upstream/master`. Do not develop directly on it.
- `main` is the default and integration branch for the complete local feature set.
- `dev` is retained as a historical branch; continue integrated development on `main`.
- Use focused topic branches when preparing isolated upstream contributions.
- General collaboration now lives in the independent private repository
  `ontron258/FreeCAD_Collaboration` (local `C:\FreeCAD\FreeCAD_Collaboration`).
  Do not add server, protocol or collaboration GUI code back into SheetMetal.
  The old feature branch preserves the extraction history; standalone installation
  uses its own Collaboration workbench and optional configured modeling addons.

Keep commits cohesive and independently understandable. In particular, avoid
leaving verified changes only on disk: the user requests that code changes be
committed and pushed to the corresponding development branch on `origin` after
verification, so the online fork remains a recovery copy. Stage only task-related
files, use a normal non-force push, and report any authentication or push failure.
In particular, avoid
mixing an isolated bug fix with a large UI or object-model change. Do not force
push, rewrite shared history, open an upstream pull request, or merge into
`master` unless the user explicitly asks.

Before editing, inspect `git status`. Preserve unrelated user changes and never
use destructive cleanup commands such as `git reset --hard`.

If `C:\FreeCAD\WORKSPACE_ORGANIZATION.md` exists, read its workspace-wide
directory, retention, cleanup and multi-agent rules in addition to this guide.
Do not assume that an absent workspace guide supplies additional instructions.

## Important feature areas

### Sheet Metal Face

`SheetMetalShapedFlangeCmd.py` implements the sketch-driven **Face** feature.
Its current responsibilities include:

- Multiple closed regions from one or more sketches.
- Add/subtract/ignore region operations.
- Panels on intersecting sketch planes.
- Automatic bends along shared collinear edges.
- Per-feature bend radius and thickness-side settings.
- Part-level thickness and defaults through the `Sheet Metal Part` `App::Part`.
- Rectangle, round, and finite-kerf tear bend reliefs.
- Cumulative feature history through `PreviousFeature`.

The internal command and serialized object names still use
`ShapedFlange` for compatibility. The user-facing command and tree labels use
`Face`. Do not casually rename serialized properties, internal command IDs, or
object names; saved FreeCAD documents depend on them.

### Material defaults and product upgrades

`SheetMetalMaterial.py` owns the material catalog and part-level material
policy. New sheet-metal parts use catalog-driven thickness, bend radius,
K-factor, and density values. Existing saved parts migrate conservatively to
manual mode. Each Sheet Metal Part exposes a read-only, unit-aware `Weight`
property calculated from its current formed Tip volume for drawings and BOMs.

The document-level `SheetMetalConfiguration` is an `App::VarSet`; its
`MaterialUpgrade` property is the predefined product configuration variable.
Parts follow it only when `FollowMaterialUpgrade` is enabled. A fixed stainless
part should therefore use `BaseMaterial = Stainless Steel` with
`FollowMaterialUpgrade = false`.

Keep shop-specific gauge tables and material defaults in this module rather
than scattering nominal values through geometry commands. New Unfold objects
inherit the owning part's K-factor as their initial manual unfold value.

`SheetMetalBaseCmd.py` promotes an `App::Part` containing a legacy BaseBend to
a Sheet Metal Part while preserving the BaseBend's existing thickness and
radius as manual part defaults. New Make Base Wall features create or promote
their owning Sheet Metal Part and consume its thickness. Their
`UseDefaultBendRadius` property controls whether they consume the part radius.

### Flat-pattern workspace and DXF export

`SheetMetalUnfoldCmd.py` contains the flat-pattern workflow:

- Actual Unfold objects stay with their owning sheet-metal parts.
- The document-level `Flat Patterns` group contains lightweight `App::Link`
  presentation objects.
- The workspace toggle saves and restores formed-object visibility.
- `AutoArrange` aligns linked flats to XY and packs them without overlap.
- `LayoutSpacing` controls spacing; disabling `AutoArrange` preserves manual
  link placement.
- DXF export uses explicit `CUT`, `BEND`, `INTERNAL`, `BEND_LABEL`, and
  `BEND_CUT` layers.
- V2 Unfold recompute persists schema-versioned physical bend occurrences in
  `BendData`, the schema number in `BendDataVersion`, the A-side normal in
  `BendReferenceNormal`, and a native local-frame centerline compound in
  `BendLines`. Keep drawing grouping, backstops, labels, and table layout out of
  the Sheet Metal model contract.

Workspace link placement is presentation state. Manufacturing export and future
drawings should reference the real Unfold geometry, not the arranged links.

### Welded mesh products

New mesh products are Sheet Metal `App::Part` containers with `MeshType = WeldedMesh`.
One `PartDesign::Body` is the primary sheet representation. Its thickness follows
`longitude diameter + latitude diameter - weld penetration`. `FormedWire`,
`FlatPattern`, `FlatSheetMetal` and `SheetMetalBody` store object names, not links
back to children. Part `Tip` remains the sheet feature; `Weight` remains wire stock
weight. Keep envelope parameter synchronization free of dependency cycles.

`SheetMetalMeshPart.py` manages ownership and imported-carrier envelopes. New
products use one longitude/latitude sketch pair; generated and editable are modes
of those same sketches. Regeneration replaces their geometry in an undo transaction.
Keep both flat representations and the sketches in one shared frame at the formed
reference panel's midplane. Their normalized XY geometry is local to that frame;
only workspace links are moved for packing.
Welded mesh is an unused-in-production prototype. Do not add compatibility or
migration paths for earlier mesh schemas; update the examples to the current model.

### Shared helpers

`SheetMetalTools.py` is widely used by the workbench. Keep changes there small
and backward compatible. A change to a shared function must retain existing
call signatures unless every caller and migration path has been checked.

## FreeCAD object-model rules

- Keep one sheet thickness at the `Sheet Metal Part` level. Different parts in
  the same document may have different thicknesses.
- A Face history may use profiles directly in one `App::Part` or inside one
  `PartDesign::Body`; do not mix scopes within a single history.
- Avoid cyclic links between an `App::Part` and its children. The current part
  `Tip` is stored as an internal object-name string for this reason.
- Each later Face feature owns the bends it introduces and may use its own bend
  radius and relief configuration.
- Preserve migration behavior for dynamic properties and enumeration options.
- Validate BRep results with `Shape.isValid()` and confirm that the result is a
  single connected solid when that is a feature requirement.

## Editing conventions

- Follow the existing Python style and license headers.
- Use `FreeCAD.Qt.translate` for user-facing strings.
- Add new commands to `InitGui.py` and register new test modules in
  `TestSheetMetal.py`.
- Put focused regression tests in `SMTests/`.
- Reuse the workbench's existing property helpers and task-panel binding helpers
  where practical.
- Preserve line endings and avoid mechanical rewrites of unrelated files.
- Do not modify or save the user's `.FCStd` documents during automated tests.

## Testing

Use FreeCAD's bundled Python, not a system Python. The current interpreter is:

```powershell
$sheetMetalPython = 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe'
& $sheetMetalPython -m unittest SMTests.testMaterialDefaults SMTests.testFlatPatternWorkspace SMTests.testShapedFlange SMTests.testFolder SMTests.testKfactor
```

FreeCAD may put the installed addon directory ahead of the repository on
`sys.path`. Before running integration tests, deploy every changed Python module
and test module to the installed addon, or otherwise verify the imported module
paths. A passing test against stale installed code is not evidence that the
repository change works.

For geometry changes, verification should normally include:

1. Python syntax/compile validation.
2. Focused unit tests for the changed geometry.
3. The full SheetMetal test selection above.
4. Valid-solid and expected-volume/topology assertions where applicable.
5. An in-memory open/recompute of the example document for saved-object
   compatibility. Close it without saving.
6. Unfold validation when a change affects bends, reliefs, thickness, or
   cumulative Face history.

FreeCAD must be restarted after deploying Python command modules because an
already imported workbench will continue using the old module objects.

## Deploying to the local FreeCAD addon

Deploy only files changed by the current work. Preserve unrelated files in the
installed addon and do not mirror-delete the directory. After copying, compare
SHA-256 hashes between repository and installed copies, then restart FreeCAD.
For the current junction-based installation, no copying is needed: repository
changes are already deployed. Verify the junction target and fresh module imports.

The repository must remain cleanly reproducible without relying on uncommitted
changes in the installed addon.

## Preparing upstream contributions

Prefer small, reviewable changes:

- DXF correctness fixes should remain separable from workspace UI changes.
- Flat-pattern workspace behavior should remain separable from automatic
  packing policy.
- The Face feature, part defaults, and bend-relief model may be proposed as a
  coherent feature stack, but document serialization and migration behavior.

Before proposing a large architecture change upstream, start a design
discussion. Add documentation and tests expected by the official project, and
rebase the topic branch onto current `upstream/master`. Keep the full local
feature set on `main` even when only selected commits are offered upstream.
