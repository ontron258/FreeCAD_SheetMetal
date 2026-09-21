# Using this SheetMetal fork

## Which repository does what?

[FreeCAD_SheetMetal](https://github.com/ontron258/FreeCAD_SheetMetal) owns CAD
modeling: Sheet Metal Parts, material defaults, Face features, bends, unfolding,
welded mesh parts, flat-pattern presentation/layout and manufacturing DXF export.

The separate private
[FreeCAD_Collaboration](https://github.com/ontron258/FreeCAD_Collaboration) owns
general FreeCAD document synchronization, the Collaboration panel, Python relay,
revision/checkpoint storage and headless validator. It is not a SheetMetal-only
server. Its [README](https://github.com/ontron258/FreeCAD_Collaboration/blob/main/README.md)
is the setup/security guide; access requires permission to the private repo.

Use SheetMetal alone for local modeling. Install both addons when collaborating
on SheetMetal models. Every client and the validator then need matching
SheetMetal code; the relay itself does not need SheetMetal or FreeCAD.

## Release Manager integration

The independent `FreeCAD_ReleaseManager` addon now owns automatic drawing-name
maintenance (`DrawingName`, `UseGroupInDrawingName`) and generic derivative lineage
(`DerivedFrom`, `VariantType`). Install it beside this fork to retain these features.
`SheetMetalNaming.py` remains a compatibility entry point; saved property names are unchanged.
Existing metadata remains stored when Release Manager is absent, and SheetMetal geometry,
mirroring, material upgrades and parameter propagation continue to work independently.

Release Manager also captures configured parts, compares manufacturing definitions,
creates TechDraw pages from preserved geometry and exports reviewed release packages.
Material catalogs, `SheetMetalConfiguration.MaterialUpgrade`, part defaults, unfolded
geometry and bend-data generation remain in SheetMetal. Its README documents installation
and the new workflow. Deploy matching addon code to collaboration clients and validator;
restart together and create a new environment checkpoint after this runtime update.

## Branches and installation

`main` is the default branch and the configured modeling checkout. It combines
the complete histories of `dev` and `feature/collaboration-prototype`, including
welded mesh parts and the extraction of collaboration into its own addon.
Continue modeling development on `main`. The previous branches remain available
for history; `master` mirrors upstream. The old embedded collaboration
implementation remains recoverable in Git history.

For a new computer, clone the selected modeling branch:

```powershell
git clone --branch main https://github.com/ontron258/FreeCAD_SheetMetal.git 'D:\Source\FreeCAD_SheetMetal'
```

Use the same tested FreeCAD build on clients and validator. The current pilot
uses FreeCAD 1.1.3 Windows x64 with bundled Python 3.11.14. This repo is only an
addon; it does not include the FreeCAD application. Install/extract FreeCAD
separately and locate the folder containing its `bin` and `Mod` directories.

To link a fresh Windows installation to the checkout (replace paths):

```powershell
$sheetMetalSource = (Resolve-Path -LiteralPath 'D:\Source\FreeCAD_SheetMetal').Path
$sheetMetalMod = (Resolve-Path -LiteralPath 'D:\Apps\FreeCAD\Mod').Path
$sheetMetalDestination = Join-Path $sheetMetalMod 'SheetMetal'
if (Test-Path -LiteralPath $sheetMetalDestination) {
    throw 'SheetMetal already exists; inspect it instead of overwriting it.'
}
New-Item -ItemType Junction -Path $sheetMetalDestination -Target $sheetMetalSource
```

Alternatively, place/link the checkout in FreeCAD's user `Mod` directory.
Do not install competing copies of the same workbench. The standard Addon
Manager listing installs upstream, not necessarily this fork/branch.

Restart FreeCAD and select **Sheet Metal**. A junction uses the checkout's code
directly, but an already running FreeCAD process retains imported Python modules.
Do not edit an installed copy instead of the source repository.

On this development machine, FreeCAD loads the `main` checkout at
`C:\FreeCAD\FreeCAD_SheetMetal` through the installation junction. The former
standalone user addon is preserved under
`C:\FreeCAD\backups\sheetmetal-installed-copy-before-main-*` and no longer
overrides that checkout. Repository updates are available on the next FreeCAD
restart without copying addon files.

## Basic modeling workflow

1. Create a Sheet Metal Part and select its material/thickness/default radius.
2. Create a base wall or sketch-driven Face feature, then add the required bends
   and subsequent Face features. Keep a part's thickness at the owning part.
3. Recompute and inspect the formed solid before manufacturing export.
4. Unfold the actual part. The document-level Flat Patterns group presents
   links to real Unfold objects; arranging those links does not change the model.
5. Use the flat-pattern/DXF export workflow for cutting and bend information.

For wire parts, see [Welded mesh parts](WELDED_MESH.md) for creating formed and
flat mesh, setting wire spacing and weld penetration, and editing the wire sketches.
Each new mesh product owns one sheet-metal Body with the full wire-envelope
thickness, formed/flat wire views, a flat sheet view and one longitude/latitude
sketch pair. Switch the part's **Representation** property to inspect each view.

### Corner Round / Chamfer

After creating the walls and bends, Ctrl-select one or more sharp outline corner
vertices on the final sheet feature. You can also select the straight, short
edges running through the sheet thickness. Choose **Sheet Metal → Corner Round /
Chamfer** from the menu or toolbar, then choose **Round** and a **Radius**, or
**Chamfer** and a **Chamfer distance** (equal setback along both sides).

Use **Select corners** to edit the set, then **Preview** to apply it. All corners
in one feature share its size; add another feature for a different size. Selecting
both ends of the same corner counts once. The feature preserves sheet thickness
and supports corners on different planar flanges of one solid. Bend edges,
already rounded corners, and selections from multiple objects are rejected.
Reduce the size if neighboring corners or bends leave insufficient room.

While selecting, the tool filters picks to valid corners on the source sheet.
An invalid or oversized preview shows a red error message in the task panel;
invalid selection rows are highlighted. If geometry creation fails, the tool
identifies and highlights the corners that cannot use the requested size.
If those corners work individually but fail together, the message identifies
the combination. Check for nearby bends or intersecting flanges as well as
the corner size. The source sheet stays visible until
the preview succeeds, and OK cannot accept a failed feature. The feature's
read-only **LastError** and **FailedCorners** properties also record the reason
and affected selections for a failed recompute.

Dimension fields follow the document's unit system: mm in standard metric
documents and inches in imperial documents. Bare numbers use the displayed
unit. Changing the unit system updates the display without changing the
physical radius or chamfer distance already stored in the feature.

Files created by the initial version may list the same corner feature twice
inside a Body. Reopening them with this version removes the repeated reference
in memory while preserving the feature and its geometry; save normally to keep
that repair in the document.

Double-click the feature to change its selection, treatment, or dimensions.
Apply this finishing feature after cumulative Face operations, and unfold the
finished result. Restart FreeCAD after installing the new command module.

Existing saved Python-feature documents depend on their original proxy classes
and properties. Keep the addon installed when reopening them; do not rename
serialized classes/properties casually. See [AGENTS.md](AGENTS.md) for the
feature contracts and [README.md](README.md) for upstream tutorials.

## Collaboration with SheetMetal models

Clone/install the independent Collaboration addon using its README, then list
the local SheetMetal checkout in BOTH client and validator configurations under
`addon_paths`. Local paths may differ across computers; code/builds must match.
Allowlist only trusted, installed Python proxy modules actually needed by the
model. Common modules for this fork are:

- `SheetMetalBaseCmd`, `SheetMetalShapedFlangeCmd`, `SheetMetalCmd`.
- `SheetMetalBoltConnectionCmd`, `SheetMetalConnectedPatternCmd`.
- `SheetMetalUnfoldCmd` for Unfold models.
- `SheetMetalWeldedMeshCmd` for welded mesh models.

Other modeling features may require additional installed proxy modules. Never
automatically import code named by an untrusted downloaded document.

Launch FreeCAD using Collaboration's `Start_Client.ps1` with the machine's
`client.json`, then select **Collaboration** to open its panel. Model with
SheetMetal; share/join/checkpoint using Collaboration. You can switch workbenches.

After a runtime/modeling-code update, restart clients and validator together.
Use **Save current as new checkpoint** to start a new session UUID for an old
model in the changed environment. Old server history remains intact; other
clients must join the new UUID. Do not force an arbitrary environment ID to
bypass compatibility checks. Save local models too; Git pushes protect code,
not `.FCStd` models or the server database.

## Current computer: configured paths

These paths describe this development computer, not portable requirements:

- Source: `C:\FreeCAD\FreeCAD_SheetMetal`.
- FreeCAD: `C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311`.
- Addon junction: `C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\Mod\SheetMetal`.
- Independent addon: `C:\FreeCAD\FreeCAD_Collaboration`.
- Private local collaboration config: `C:\FreeCAD\tmp\collaboration-standalone`.

The local pilot uses `http://127.0.0.1:8765`. That address is only this computer;
it is not the future desktop LAN server. Desktop deployment, credentials,
firewall allowlisting and transport encryption must be configured separately.

### If PowerShell blocks the launcher script

An error that "running scripts is disabled" is PowerShell blocking the `.ps1`
file, not FreeCAD rejecting the addon. Launch with interactive commands instead;
no execution-policy change is needed:

```powershell
$env:FREECAD_COLLABORATION_CONFIG = 'C:\FreeCAD\tmp\collaboration-standalone\client.json'
Start-Process 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\freecad.exe'
```

This sets configuration in the current PowerShell process and its subsequently
launched children, not globally. Use your own config/application paths on another
computer. Select **Collaboration** to open the panel after FreeCAD starts. The
collaboration README also includes direct server/validator launch alternatives.

## Development and verification

Follow [AGENTS.md](AGENTS.md). Run from this checkout with FreeCAD's Python:

```powershell
& 'C:\FreeCAD\FreeCAD_1.1.3-Windows-x86_64-py311\bin\python.exe' -m unittest SMTests.testMaterialDefaults SMTests.testFlatPatternWorkspace SMTests.testShapedFlange SMTests.testFolder SMTests.testKfactor
```

Verify imported module paths point at this checkout or its junction. Automated
tests must never save/modify user documents. Commit and push verified modeling
changes to this fork, and collaboration changes to its own repo. Use normal
non-force pushes; do not merge into `master` or rewrite history without approval.
