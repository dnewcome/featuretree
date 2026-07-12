# featuretree

**Emit an editable feature tree — in FreeCAD *and* Onshape — from a small neutral feature-IR, round-trip human edits back by name, and render the same IR to a build123d solid.**

A neutral *file* (STEP/STL) loses the parametric feature tree: it imports into FreeCAD as one
frozen solid you can't edit by operation. `featuretree` keeps the tree. You author a design once as
a small **feature IR** (named, ordered operations), and an emitter re-authors it in FreeCAD's *own*
feature vocabulary (`PartDesign::Sketch / Pad / Pocket / Fillet`), with each object's `Label` set to
your feature name. Open the `.FCStd` and the operations are right there in the left-panel tree,
editable. Human edits read back **by name**, so they survive rebuilds.

```
   ir.py  (the DSL, your single source of truth)        gen.py  ── emit ──►  <part>.FCStd
   named features · named params · symbolic geometry                         (native tree,
   queries (no kernel edge ids)                          roundtrip.py ◄─ read ─ human edits, by name)
```

This is packaged as a [Claude Code](https://claude.com/claude-code) **skill**, but the Python is
plain and runs standalone — see [Use without Claude Code](#use-without-claude-code).

## Why

The thing that makes a feature tree survive a round-trip is that geometry is referenced
*symbolically* — by feature name and queries — never by unstable kernel edge/face ids. That's the
wall that kills neutral feature-file formats. The IR sidesteps it:

- every feature has a stable, human-meaningful `name` → the target object's `Label`, so a human edit
  is matched back **by name**, not by geometry;
- parameters are named values, not positions in a blob;
- edge/face selection is a **query** re-resolved against live geometry at every build, never a stored
  kernel id.

## Requirements

- **FreeCAD 1.0+ AppImage.** FreeCAD is driven head-less through its own bundled Python (3.11) via
  `freecadcmd` — build123d / OCP can't author a *native* feature tree. The runner auto-locates the
  AppImage at `/opt`, or one-time-extracts it and caches the result. Override with the
  `FREECAD_APPIMAGE` (path to the AppImage) or `FREECAD_CMD` (path to a `freecadcmd` binary)
  environment variables.
- **Python 3** for the host-side scripts (`gen.py` / `roundtrip.py`).
- **For the Onshape backend (optional):** `pip install onpy` and an Onshape API key (access +
  secret) from <https://dev-portal.onshape.com>. See [Onshape backend](#onshape-backend).

## Install

As a Claude Code skill:

```bash
git clone https://github.com/dnewcome/featuretree.git ~/.claude/skills/featuretree
```

Claude Code discovers it automatically; ask it to use the `featuretree` skill when you want a
FreeCAD file that opens with its operations in the tree.

## Quickstart

```bash
S=~/.claude/skills/featuretree
python3 $S/gen.py --sample plate         # built-in: 40x30x10 plate + hole  -> $S/out/plate.FCStd
python3 $S/gen.py --sample poly          # exercises the polygon-profile primitive
python3 $S/gen.py mypart.ir.json out/mypart.FCStd     # emit a spec you authored
python3 $S/roundtrip.py out/mypart.FCStd              # read the tree / params back
python3 $S/roundtrip.py out/mypart.FCStd edits.json   # apply a named edit, re-save, report
```

Authoring a part with the DSL (produce a JSON-able dict, write it, emit it):

```python
import sys; sys.path.insert(0, "/path/to/featuretree")
import ir, json
spec = ir.part("bracket",
    ir.sketch("profile", polys=[[(0,0),(40,0),(40,14),(16,14),(16,30),(0,30)]]),  # outer wire
    ir.pad("body", "profile", length=6),
    ir.sketch("holes", circles=[(8,8,2.6)]),
    ir.pocket("drill", "holes", through=True),
)
json.dump(spec, open("bracket.ir.json", "w"))            # then: python3 gen.py bracket.ir.json
```

## The IR (see [`ir.py`](ir.py))

- `sketch(name, plane="XY"|"XZ", circles=[(cx,cy,r)], rects=[(w,h,cx,cy)], polys=[wire,...], on=None)`
  — `polys`: first wire = outer profile, following wires = holes (one sketch → one pad gives a plate
  with holes); a wire vertex may carry a DXF **bulge** `(x,y,bulge)` for a circular arc.
  `on={"face_of": feat, "side": "top"|"bottom"}` attaches the sketch to a face chosen by **query**
  (circles, rects, *and* polys).
- `pad(name, sketch, length, symmetric=False)` — deterministic +Z growth (so both backends agree).
- `pocket(name, sketch, through=True, length=None)`
- `revolve(name, sketch, angle=360)` — revolve an XZ profile about Z (wheels, bosses, nozzles).
- `prism_cut(name, origin, normal, xdir, depth, polys=[wire,...])` — subtract a 2D profile extruded
  along an **arbitrary axis** at an **arbitrary location**. One primitive for any placed cut: a
  floor / through pocket along the main axis *or* a cross-axis hole ⊥ to it. This is what multi-axis
  STEP recovery emits; both backends build it identically.
- `polar_pocket(name, radius, length, mount_r, z, count, phase)` — a ring of tangent bores.
- `fillet(name, radius, select={"circles": "top_outer"})` — edges chosen by **query**, re-resolved to
  live `EdgeN` every build (never a stored kernel id — the topological-naming sidestep).
- `part(name, *features)` → the spec. `update_from_freecad(spec, params)` flows read-back edits in.

## build123d backend

The same IR also renders straight to a **build123d** solid, in the caller's own Python — no FreeCAD
process. build123d and FreeCAD share the OpenCASCADE kernel, so the IR yields **identical geometry**
either way: the `plate` sample is 11497.3 mm³ from both backends (Δ = 0.0). Author the design once as
IR and get *both* the editable parametric tree (FreeCAD/Onshape) *and* a watertight solid you can
mesh / simulate / interference-check — with no second, hand-maintained model to drift.

```python
import ir, b3d_emit
part, res = b3d_emit.emit(ir.SAMPLES["plate"]())   # part is a build123d Solid
print(res["volume"], "mm^3")                        # 11497.3, same as FreeCAD
```

```bash
python3 b3d_emit.py --sample plate out/plate.stl    # IR -> watertight .stl
```

Coverage mirrors `fc_build.py`: sketches (circles / rects / polygons-with-holes, straight + arc) on
XY / XZ or a part's top/bottom face, pad (± midplane), pocket (through / blind), revolve, `prism_cut`
(placed profile-along-any-axis), polar pockets, and fillet by the **same edge query** the IR stores
(resolved against live geometry, no kernel ids).

## STEP → IR (feature recognition) — round-trip engineering

A STEP/STL from a vendor, a 3D scan, a colleague, or a decade-old archive is a **frozen solid**: the
parametric feature tree is gone, so it imports as one lump you can't edit by *operation*. Change a
pocket's depth or a hole's diameter and you're pushing vertices, not editing the design's intent.

`step_recognize.py` **recovers a feature tree from the dumb B-rep** — and, paired with the by-name
round-trip below, that closes the engineering loop: **import a frozen solid → recover a named,
parametric tree → edit an operation in FreeCAD (or build123d) → re-emit → and it's *proven* to still
be the same part minus your intended change.** The neutral file becomes editable again, without
trusting a black-box recognizer that might quietly be wrong — because every recovery is checked by
reconstruction (below). That is the motivation for the whole tool: neutral files are how CAD data
actually moves between people and programs; this makes them *parametric* again on the way in.

```python
import step_recognize
spec, report = step_recognize.recognize("part.step")    # spec is a featuretree IR
print(report["verified"], report["dvol_pct"], report.get("recovered"))
# True 0.1 {'pockets': 11, 'holes': 10, 'residual_lumps': 2}   # NIST CTC-01
```

```bash
python3 step_recognize.py part.step --stl out.stl           # recover + write a viewable solid
python3 step_recognize.py part.step --fcstd out.FCStd       # recover + write an editable FreeCAD tree
python3 step_recognize.py part.step --emit out.ir.json      # recover + write the IR
python3 step_recognize.py --selftest                        # generate fixtures, assert (quick CI)
python3 -m pytest tests/                                     # full test suite (emit + recognize)
```

### Recovery strategies

The core idea: **most machined/printed parts are a 2D profile swept along an axis** — extruded, or
revolved — **plus holes and pockets.** So recognition is *cross-sectional*, in layers, each proven
before the next is trusted:

1. **Dispatch — extrude vs revolve.** Find the axis the solid is a prismatic extrusion along (every
   face planar-⊥, planar-∥, or a cylinder ∥ to it) or a body of revolution about (rotating it leaves
   it unchanged). Orientation-agnostic: any X/Y/Z or face-normal axis is rotated onto Z.
2. **Section-based outline.** The outline + through-holes come from a *cross-section*, sampled at a
   few heights (not a single end face) so a stepped/fragmented end — or a section landing exactly on
   a feature plane — doesn't derail it. The profile keeps **straight edges *and* circular arcs**.
3. **Through vs blind.** An inner wire is emitted as a through-hole only if it's present near **both**
   faces; a blind pocket (floor below the section) is *not* drilled through — over-cutting can't be
   undone — but deferred to recovery. Through-holes may be **any shape** (circle, slot/obround, poly).
4. **Multi-axis recovery.** When the base extrude doesn't verify, the machined interior is still in
   the **residual** (`recovered − original`), which is carved feature by feature:
   - **(A) floor pockets** — every significant intermediate plane ⊥ the axis is a pocket *floor*;
     cut it (keeping islands/bosses standing via outer-minus-inner-wire profiles);
   - **(B) residual carve loop** — whatever's left breaks into separate lumps, and *each lump is
     itself a 2D profile extruded along **its own** axis* — a through-web pocket ∥ the main axis, a
     **cross-axis hole** ⊥ it. Recognize each and subtract it as a `prism_cut` (a placed
     profile-along-an-axis), looping until the residual vanishes.
5. **Verification-driven axis choice.** Several axes can *look* prismatic (a cross-hole makes its own
   axis look clean), so recognize() gathers a candidate per axis and lets **verification** pick the
   winner: first a base that verifies on its own (the simplest clean extrude), else the first whose
   recovery verifies.
6. **Self-verification (the honesty).** The recovered IR is re-emitted through `b3d_emit` and its
   volume + (rotation-tolerant) bounding box compared to the original. Every result is **VERIFIED**
   (Δvol ≈ 0 — provably the same solid within tolerance, now an editable tree) or **PARTIAL** with
   the residual reported. This is what tames the non-uniqueness of feature recognition: *any*
   decomposition that reconstructs the solid is accepted — geometric equivalence, not a guess at the
   designer's exact operations — and anything that doesn't reconstruct is rejected, never faked.

On the NIST CTC-01 test part (multi-level pockets, through-web windows, 12 cross-holes, 8 chamfers)
this takes a 139%-off base extrude to a **verified** 51-feature tree, and that tree re-emits to the
**identical volume in both build123d *and* FreeCAD** — the recovered part is genuinely editable in
either.

### Drawbacks & limits (surfaced, never silently wrong)

- **Equivalence, not intent.** Verification certifies the recovered solid *matches* within tolerance
  — not that the operations are the ones the designer used. A two-level pocket may come back as
  several `prism_cut`s rather than "ledge + bore"; it's the same geometry, editable, but not
  necessarily the same *history*. Recognition is inference and non-unique in general.
- **Edge fillets/chamfers aren't in the vocabulary** — they're edge blends, not swept profiles — so
  they're left as **sub-tolerance residual**. A chamfered part can verify within tolerance while the
  recovered tree has sharp edges (`recovered.residual_lumps` and a warning disclose it). NIST's 8
  chamfers are its whole ~0.1% residual.
- **Additive bosses can't be recovered** (recovery only *subtracts* from the outline envelope). A
  raised post on a base is flagged PARTIAL, not faked.
- **Splines / ellipses / lofts / sweeps / freeform → PARTIAL.** No faithful sketch-and-pad tree
  exists; the verifier rejects rather than guess.
- **Pathological tangencies can over-cut.** A cross-hole exactly coincident with a pocket floor
  fragments that floor and the recovery may not close fully — but the verifier catches it and reports
  PARTIAL rather than shipping a wrong tree. It degrades to honest, not to silently-incorrect.
- **Cost.** Recovery runs repeated boolean diffs and per-axis attempts — **seconds** per complex
  part, not milliseconds. It only kicks in when the base doesn't verify (clean prismatic parts are
  fast). Disable with `recognize(..., recover=False)` for the base-only best-effort.

### Bridging IN from build123d (the reverse direction)

build123d bakes operations into a final solid, so you can't *extract* its tree. To bring an existing
build123d part in, author its operations as IR using the **same named constants** your build123d
script uses (profile points, thickness, hole positions), so both paths describe one design —
then `b3d_emit` regenerates an equivalent solid to confirm parity. Or export it to STEP and try
`step_recognize` (above) — it verifies whether the inferred tree actually reproduces the part.
Geometry that's a mesh boolean (no clean sketch/pad) can't be a feature tree — export those as
STEP/STL and import as a single solid, and say so.

## How it runs

| file | runs under | role |
|------|-----------|------|
| `ir.py` | any Python 3 | the DSL / IR — single source of truth, plain JSON-able dicts |
| `b3d_emit.py` | host Python 3 | render an IR spec → a build123d Solid (+ `.stl`) in-process, no FreeCAD |
| `step_recognize.py` | host Python 3 | recover an IR from a STEP B-rep (2.5D-prismatic), self-verified by re-emit |
| `gen.py` | host Python 3 | emit an IR spec → `.FCStd` (+ `.stl`); shells out to FreeCAD |
| `roundtrip.py` | host Python 3 | read the tree / params back; optionally apply named edits |
| `runner.py` | host Python 3 | locate / extract `freecadcmd`, run a script under it |
| `fc_build.py` | FreeCAD's Python 3.11 | build the native PartDesign tree |
| `fc_read.py` | FreeCAD's Python 3.11 | read labels / params back out |
| `fc_common.py` | FreeCAD's Python 3.11 | shared FreeCAD-side helpers |
| `onshape_client.py` | host Python 3 | Onshape REST client (HMAC) — create a doc, run FeatureScript |
| `onshape_emit.py` | host Python 3 | emit an IR spec → an Onshape Part Studio (via `onpy`) |

Data passes to the FreeCAD-side scripts via **env vars**, never argv — `freecadcmd` treats extra path
arguments as documents to open.

## Onshape backend

The same IR also drives **Onshape** — one IR emits to FreeCAD *and* a live Onshape Part Studio, so the
design opens, editable, in cloud CAD too. Onshape geometry is created through
[`onpy`](https://github.com/kyle-tennison/onpy) (a maintained Python Onshape API whose BTM
serialization is known-correct); `onshape_client.py` is a small stdlib-only HMAC REST client used to
create the document and run FeatureScript.

```python
import sys; sys.path.insert(0, "/path/to/featuretree")
import ir, onshape_client as oc, onshape_emit
spec = ir.SAMPLES["plate"]()                          # or your own ir.part(...)
doc = oc.create_document("my-part", public=True)      # free accounts: public docs only
onshape_emit.emit(spec, doc["did"])
print("https://cad.onshape.com/documents/" + doc["did"])
```

**Auth** (two credentials, both kept out of the repo — same Onshape key pair):

- `onshape_client.py` reads `ONSHAPE_ACCESS_KEY` / `ONSHAPE_SECRET_KEY` from the environment
  (HMAC-SHA256 signing, `Accept: */*` — *not* `application/json`, which forces a regen-hostile
  serialization).
- `onpy` reads `~/.onpy/config.json` → `{"dev_access": "...", "dev_secret": "..."}`. `onpy.configure()`
  is the *interactive* setup prompt — skip it once that file exists.

### Onshape scope / gotchas

- **Working:** sketches (polygons / circles / rects) on the Top plane → **Pad** (extrude) and
  **Pocket** (subtract). Geometry is exact (a metric part round-trips to the right millimetre).
- **Units:** onpy's `metric` system is **meters**, so the emitter scales the mm IR by `0.001`.
- **Sketches arrive under-defined** ("not fully defined" / blue) — the API places geometry by
  coordinate with no constraints. The solid is correct and won't drift; the IR is the source of truth,
  so this is a property of the generated *view*, not a defect. (Fully-defining would mean authoring
  constraints in raw BTM, which onpy doesn't expose.)
- **Speed:** onpy re-solves the sketch on every entity add (a round-trip per line/circle), so dense
  profiles (100+ segments) are slow — prefer `circles` over many-sided polygons and simplify outlines.
- **Free Onshape accounts can only create public documents.**
- **Not yet:** fillets, face-attached sketches, non-Top planes — build123d / FreeCAD still cover those.

## Use without Claude Code

Nothing here depends on Claude Code at runtime. Clone anywhere, point `sys.path` at the directory (or
run `gen.py` / `roundtrip.py` directly), and make sure the FreeCAD AppImage is locatable per
[Requirements](#requirements).

## Scope / honesty

- **Working:** XY / XZ sketches (rect / circle / polygon + profile-with-holes), face-attached sketches
  (circle / rect / poly), Pad, Pocket (through / blind), Revolve, `prism_cut` (a profile extruded
  along an arbitrary axis at an arbitrary location — the primitive multi-axis recovery emits), query
  fillets, polar pocket patterns; parameter round-trip (lengths, radii) by name. FreeCAD *and*
  build123d emit the same geometry from the same IR (verified equal on the recovered NIST tree).
- **STEP → IR recognition:** extrude / revolve in any orientation, arcs, any-shape through-holes, and
  **multi-axis recovery** (floor / through-web pockets + cross-axis holes) — self-verified by re-emit.
  See [STEP → IR](#step--ir-feature-recognition--round-trip-engineering).
- **Onshape backend:** sketches (polys / circles / rects) → Pad / Pocket via `onpy`; geometry
  exact. Sketches arrive under-defined; fillets / face-attach / non-Top planes / `prism_cut` not yet.
  See [Onshape backend](#onshape-backend).
- **Deferred:** non-XY/XZ unattached planes, richer edge selectors (by-radius / position / count),
  edge fillet/chamfer *recognition*, other backends (Fusion API / SolidWorks macro).
- A SolidWorks `.SLDPRT` can't be written on Linux — that backend would emit a macro.

## License

[MIT](LICENSE)
