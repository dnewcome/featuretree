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

- `sketch(name, plane="XY", circles=[(cx,cy,r)], rects=[(w,h,cx,cy)], polys=[wire,...], on=None)`
  — `polys`: first wire = outer profile, following wires = holes (one sketch → one pad gives a plate
  with holes). `on={"face_of": feat, "side": "top"|"bottom"}` attaches the sketch to a face chosen by
  **query** (circles only, v0).
- `pad(name, sketch, length, symmetric=False)`
- `pocket(name, sketch, through=True, length=None)`
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

Coverage mirrors `fc_build.py`: sketches (circles / rects / polygons-with-holes) on XY or a part's
top/bottom face, pad (± midplane), pocket (through / blind), and fillet by the **same edge query**
the IR stores (resolved against live geometry, no kernel ids).

## STEP → IR (feature recognition)

A STEP file is a dumb B-rep — no feature tree — so you can't *convert* it to an IR, only *infer*
one. `step_recognize.py` does that for the **2.5D-prismatic class** the IR was built for: it
classifies the solid's faces with the OpenCASCADE kernel (largest planar base face → outline + pad;
concave cylindrical faces with a Z axis → circular through/blind pockets) and emits the IR.

The result is **self-verified**: the recognized IR is re-emitted through `b3d_emit` and its volume +
bounding box are compared to the original STEP. So every recognition is either **VERIFIED** (Δvol ≈ 0
— provably the same solid, now an editable tree) or flagged **PARTIAL** with the residual, in which
case you fall back to importing the STEP as one solid. It never fakes a tree.

```python
import step_recognize
spec, report = step_recognize.recognize("plate.step")   # spec is a featuretree IR
print(report["verified"], report["dvol_pct"])           # True 0.0
```

```bash
python3 step_recognize.py plate.step --emit plate.ir.json   # recover + write the IR
python3 step_recognize.py --selftest                        # generate fixtures, assert (quick CI)
python3 -m pytest tests/                                     # full test suite (emit + recognize)
```

Handled: extrudes and revolves in **any orientation**, profiles with **straight edges + arcs**,
and through-holes of **any shape** (circle, slot/obround, polygon). **Out of scope** (surfaced as
PARTIAL, never silently wrong): fillets/chamfers, additive bosses, non-circular *blind* pockets,
and lofts / sweeps / freeform — there is no faithful feature tree to recover there (recognition is
inference, non-unique in general), so the verifier rejects them rather than guess.

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

- **Working:** XY sketches (rect / circle / polygon + profile-with-holes), face-attached circle
  sketches, Pad, Pocket (through / blind), query fillets; parameter round-trip (lengths, radii) by
  name.
- **Onshape backend (new):** sketches (polys / circles / rects) → Pad / Pocket via `onpy`; geometry
  exact. Sketches arrive under-defined; fillets / face-attach / non-Top planes not yet. See
  [Onshape backend](#onshape-backend).
- **Deferred:** non-XY unattached planes, polygon / rect face-attached sketches, richer edge selectors
  (by-radius / position / count), other backends (Fusion API / SolidWorks macro).
- A SolidWorks `.SLDPRT` can't be written on Linux — that backend would emit a macro.

## License

[MIT](LICENSE)
