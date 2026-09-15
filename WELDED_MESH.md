# Welded mesh parts

Select a **planar face on a sheet-metal carrier**, then choose **Create Welded
Mesh Part** in the Sheet Metal workbench. One **App::Part** owns the complete
product: one **Sheet Metal Body**, **Flat Sheet Metal**, **Formed Wires**,
**Flat Wires**, mesh settings, and one longitude/latitude sketch pair.
The Body is the primary modelling representation; its current Tip drives the
derived representations. Use the part's **Representation** property to switch views.

The developed base panel stays aligned with the selected formed base panel.
Flat sheet metal, flat wires and their sketches share that panel's midplane;
switching representations does not relocate the part. Sketch geometry uses a
local XY grid, while its placement follows the Body. Only the Flat Pattern
Workspace arranges separate presentation links for inspection and packing.

The Body represents the nominal full wire envelope. Its thickness is driven by
wire diameters and weld penetration, rather than by a sheet gauge. An existing
Body retains its features. A carrier built with Part features keeps its editable
history inside the product and gets a Body base feature that constructs the full
envelope about its original mid-surface. This is a prototype; recreate earlier mesh
examples with the current macros rather than migrating old mesh schemas.

Run `Macros/WeldedMeshDemo.FCMacro` to create a sample in a new document.

For a more complete example, run `Macros/WeldedMeshTrayDemo.FCMacro`. It creates
an unsaved document with a constrained 180 x 120 mm floor sketch, three editable
flanges (42 mm back straight leg, 30 mm side straight legs, R6 bends), and four
native Body pockets: a rectangular floor window, a round opening, a front notch and
a back handle slot. The mesh uses 2 mm wire, an initial 10 mm pitch and 0.3 mm
weld penetration. Both flat wire sketches are editable, and one wire is trimmed
4 mm at one end. Expand **Sheet Metal Body** for the modelling history; use
**Toggle Flat Pattern Workspace** to inspect the mesh preparation. The product's
**DemoNotes** property explains the example and how to regenerate its grid.

The Body defines the formed panels, cylindrical bends and developed layout.
Editing it or extending its history recomputes the mesh from the current Tip.
The part's sheet thickness follows the mesh envelope; weight remains the wire
stock weight, not the solid sheet-envelope weight.

## Parameters

Double-click **Formed Wires** or **Mesh Parameters and Boundary** to open the
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
  wires with 0.3 mm penetration produce a 3.7 mm Body and flat sheet envelope.
- **LayerOrder** chooses which family lies toward the selected face normal.
  The envelope is centred at the carrier's mid-thickness by default;
  **ReferenceOffset** moves the formed wires along that normal. The flat output
  remains centred on the base panel's midplane independently of this offset.
- **KFactor** is the ANSI development value, inherited initially from the
  owning Sheet Metal Part. Development uses the full envelope thickness.
- **Centrelines** provides a lighter preview. Choose **Solid wires** for solid
  geometry exports. Each wire is a separate solid in a compound.

**Flat Wires** and **Flat Sheet Metal** participate in **Toggle Flat Pattern Workspace** alongside
ordinary sheet-metal unfolds. Workspace links and their arrangement are
presentation state; the original flat object and sketches remain the model
inputs and outputs.

## Editing individual wires

Select the mesh product or a mesh child and choose **Make Mesh Wires Editable**.
The command switches the existing pair to manual mode and shows the flat
boundary. It does not create another pair. Double-click either sketch to
trim, delete, extend or add wires. Each non-construction straight segment is
one wire. Construction geometry is ignored. Editing a generated sketch directly
also switches it to manual mode before the next recompute.

Manual geometry is never regenerated during a recompute. Changing the carrier,
diameters, penetration or material updates the derived representations while
preserving the sketch geometry. Pitch/count controls govern only generated
patterns and are disabled in the task panel while using editable sketches.

To return to a generated layout, right-click **Formed Wires** and choose
**Regenerate wire pattern**. This replaces the geometry in the same two sketches
with the requested pitch/count pattern. **Undo** restores the manual geometry.

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

`SheetMetalMeshPart.py` owns product/Body organization and the sheet envelope.
`SheetMetalWeldedMeshCmd.py` owns the document features, the active sketch pair,
conversion commands and task panel. The dependency graph runs from the carrier
through mesh settings and input sketches to the formed and flat outputs.
Containers keep output identities as names, avoiding child-to-container link
cycles. The transient surface map is rebuilt after document restoration.

`SMTests.testWeldedMesh` covers geometry, layer offsets, openings, pitch/count,
editable sketches, diagonal wires, opposing bends, invalid inputs, compound
mass, save/reopen, undo and flat-workspace recognition.
