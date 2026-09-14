# Welded mesh parts

Select a **planar face on a sheet-metal carrier**, then choose **Create Welded
Mesh Part** in the Sheet Metal workbench. The new product has a formed mesh,
a flat mesh, a carrier boundary, and two generated wire sketches. The source
carrier remains a separate, linked object and is hidden on creation.

Run `Macros/WeldedMeshDemo.FCMacro` to create a sample in a new document.

The carrier defines the formed panels, cylindrical bends and developed layout.
Editing its geometry recomputes the mesh. The selected source feature is fixed;
if a later history feature replaces it, update the `Source` reference on **Mesh
Parameters and Boundary**. The original carrier's material properties are not
changed by mesh creation.

## Parameters

Double-click **Formed Mesh** or **Mesh Parameters and Boundary** to open the
task panel. Use **Update preview** to apply several parameter changes together.
The same values are also available in the property editor.

- Each family has a diameter, pitch/count mode, edge margin and pitch phase.
  **SameDiameter** uses the longitude diameter for both families.
- Pitch means centre-to-centre spacing. The initial grid is centred inside the
  bounds after applying the transverse margin; phase shifts that grid. Wire
  endpoints are clipped to the actual outline and openings. A margin controls
  the positions of wire rows, not a setback at every trimmed endpoint.
- Count includes both end rows, with evenly spaced centres inside the margins.
  A single wire is centred. Openings can split one row into several wire pieces,
  so the reported piece count can exceed the specified row count.
- **WeldPenetration** reduces centreline separation and total thickness. With
  diameters `dL`, `dT` and penetration `p`, the envelope is `dL + dT - p` thick.
  Penetration must be nonnegative and smaller than either diameter. Two 2 mm
  wires with 0.3 mm penetration produce a 3.7 mm envelope.
- **LayerOrder** chooses which family lies toward the selected face normal.
  The envelope is centred at the carrier's mid-thickness by default;
  **ReferenceOffset** moves it along that normal. The flat output is centred
  about its own XY plane independently of this carrier offset.
- **KFactor** is the ANSI carrier development value, inherited initially from
  the owning Sheet Metal Part. It is independent of welded mesh thickness.
- **Centrelines** provides a lighter preview. Choose **Solid wires** for solid
  geometry exports. Each wire is a separate solid in a compound.

**Flat Mesh** participates in **Toggle Flat Pattern Workspace** alongside
ordinary sheet-metal unfolds. Workspace links and their arrangement are
presentation state; the original flat object and sketches remain the model
inputs and outputs.

## Editing individual wires

Select the mesh product or a mesh child and choose **Make Mesh Wires Editable**.
The command creates two ordinary Sketcher sketches, switches the mesh inputs
to them and shows the flat boundary. Double-click either editable sketch to
trim, delete, extend or add wires. Each non-construction straight segment is
one wire. Construction geometry is ignored.

Manual geometry is never regenerated during a recompute. Changing the carrier,
diameters, penetration or material updates the derived representations while
preserving the sketch geometry. Pitch/count controls govern only generated
patterns and are disabled in the task panel while using editable sketches.

To return to a generated layout, right-click **Formed Mesh** and choose
**Regenerate wire pattern (keep edited sketches)**. This reconnects the original
generated inputs and retains all edited sketches in the product. The command
is undoable. Converting again makes fresh editable copies.

An oblique line in either sketch creates a wire in that family's layer. At a
cylindrical bend, an oblique crossing becomes a helical path. Same-layer wire
overlaps are reported in **Warnings**; they are not automatically separated
or converted into additional weld layers.

## Scope and manufacturing interpretation

The carrier must be a valid, single, constant-thickness sheet-metal solid whose
selected side develops into nonoverlapping planar panels and cylindrical bend
regions. Multiple and opposing cylindrical bends are supported. Curved sketch
elements, wires outside the developed carrier, and nondevelopable carriers are
reported as errors. Wire paths cannot currently extend beyond the reference
boundary; enlarge the carrier before extending a wire past it.

The geometric wire sweeps use layer offsets on the carrier. Different layers
can consequently have different formed lengths around bends. This is a
geometric model of mesh welded flat and then formed, not a simulation of wire
plasticity, weld distortion or exact manufacturing compensation. Validate the
carrier allowance against the shop's forming process before using preparation
dimensions for production. Oblique bend curves use spline interpolation.

`FlatWireLength` describes the edited preparation. `FormedWireLength` describes
the derived geometric paths. Weight uses flat stock length, circular area and
material density; it does not subtract the artificial overlapping volume at
welds. Individual welded-wire deformation and a fused weld solid are outside
this first version. Collision reporting currently checks same-layer overlap
in the flat preparation, not all possible collisions after forming.

The initial implementation limits the mesh to 2000 wire pieces. Large compound
models can still take time to recompute; use centrelines while adjusting them.

## Implementation and validation

`SheetMetalNewUnfolder.unfold(..., face_map=...)` optionally exposes transient
panel transforms and complete bend boundaries. The existing return values and
ordinary unfold callers are unchanged. `SheetMetalMeshGeometry.py` maps flat
line segments to those patches and sweeps the wires.

`SheetMetalWeldedMeshCmd.py` owns the document features, generated sketches,
conversion commands and task panel. The dependency graph runs from the carrier
through mesh settings and input sketches to the formed and flat outputs.
Containers keep output identities as names, avoiding child-to-container link
cycles. The transient surface map is rebuilt after document restoration.

`SMTests.testWeldedMesh` covers geometry, layer offsets, openings, pitch/count,
editable sketches, diagonal wires, opposing bends, invalid inputs, compound
mass, save/reopen, undo and flat-workspace recognition.
