"""Feature-tree composer: ordered positioned primitives combined with
ADD/SUBTRACT/INTERSECT into solid bodies -- the "flexibility" layer on top of
shapes.py's single-shape templates.

A *body* is one solid part: an ordered list of features folded together with
boolean ops. A *model* is one or more bodies (an assembly). Each feature is:

    {"type": "box"|"rod"|"tube"|"link"|"coil"|"plate"|"sketch"|"revolve",
     "params": {...type-specific, see FEATURE_SCHEMA...},
     "position": {"x":.., "y":.., "z":..},
     "rotation": {"rx":.., "ry":.., "rz":..},   # degrees, build123d's
                                                  # intrinsic order (rx about
                                                  # world X, then ry about
                                                  # the new Y, then rz about
                                                  # the newest Z -- matters
                                                  # once more than one axis
                                                  # is non-zero)
     "mode": "add"|"subtract"|"intersect",       # feature 0 is always "add"
     "suppressed": bool,                          # skipped entirely if true
     "pattern": {...optional, see _pattern_transforms...}}

Every primitive is built in its own LOCAL frame (centered on its own axis for
box/rod/tube/link, base-anchored at z=0 for plate/sketch/revolve -- whichever
is the natural convention for that shape), then placed by one or more
Location transforms (more than one when the feature is patterned), then folded
into the running result with the requested boolean op.

As with shapes.py, every value here is validated to a plain float/bool/
point-list before it is ever formatted into source -- no request text is
interpolated as code.
"""

from __future__ import annotations

from shapes import _parse_points, _soften_helper_source

MAX_FEATURES = 40          # across the whole model, not per body
MAX_BODIES = 8
MAX_PATTERN_COUNT = 60

# Per-type param schema: (label, kind, default, min, max, step). Position,
# rotation, mode, and pattern are handled generically for every type.
FEATURE_SCHEMA: dict[str, dict] = {
    "box": {
        "label": "Box",
        "params": {
            "length": ("Length (mm)", "float", 20.0, 1.0, 300.0, 0.5),
            "width": ("Width (mm)", "float", 15.0, 1.0, 300.0, 0.5),
            "height": ("Height (mm)", "float", 8.0, 0.5, 300.0, 0.5),
        },
    },
    "rod": {
        "label": "Rod (cylinder)",
        "params": {
            "length": ("Length (mm)", "float", 25.0, 1.0, 300.0, 0.5),
            "radius": ("Radius (mm)", "float", 1.5, 0.25, 50.0, 0.25),
        },
    },
    "tube": {
        "label": "Tube (hollow cylinder / bushing)",
        "params": {
            "length": ("Length (mm)", "float", 12.0, 0.5, 300.0, 0.5),
            "outer_radius": ("Outer radius (mm)", "float", 5.0, 0.5, 60.0, 0.25),
            "inner_radius": ("Inner radius (mm)", "float", 3.0, 0.0, 59.0, 0.25),
        },
    },
    "link": {
        "label": "Tapered link (dog-bone, bossed pivots)",
        "params": {
            "length": ("Length (mm)", "float", 30.0, 4.0, 300.0, 0.5),
            "end_width": ("End width (mm)", "float", 5.0, 1.5, 60.0, 0.5),
            "waist_width": ("Waist width (mm)", "float", 3.0, 0.8, 40.0, 0.5),
            "thickness": ("Thickness (mm)", "float", 2.0, 0.5, 20.0, 0.25),
            "boss_dia": ("Boss diameter (mm, 0 = none)", "float", 3.5, 0.0, 30.0, 0.25),
            "boss_height": ("Boss height (mm)", "float", 0.9, 0.0, 10.0, 0.1),
            "hole_dia": ("Pin hole diameter (mm, 0 = none)", "float", 2.2, 0.0, 20.0, 0.25),
            "hole_start": ("Hole at start", "bool", True, None, None, None),
            "hole_end": ("Hole at end", "bool", True, None, None, None),
        },
    },
    "coil": {
        "label": "Coil spring",
        "params": {
            "length": ("Free length (mm)", "float", 20.0, 3.0, 200.0, 0.5),
            "coil_radius": ("Coil radius (mm)", "float", 2.5, 0.5, 40.0, 0.25),
            "wire_radius": ("Wire radius (mm)", "float", 0.4, 0.1, 5.0, 0.05),
            "turns": ("Turns", "float", 6.0, 1.0, 40.0, 1.0),
        },
    },
    "plate": {
        "label": "Plate (rounded rect, corner holes)",
        "params": {
            "length": ("Length (mm)", "float", 40.0, 4.0, 400.0, 0.5),
            "width": ("Width (mm)", "float", 25.0, 4.0, 400.0, 0.5),
            "thickness": ("Thickness (mm)", "float", 3.0, 0.5, 30.0, 0.5),
            "corner_radius": ("Corner radius (mm)", "float", 3.0, 0.0, 40.0, 0.25),
            "hole_dia": ("Corner hole diameter (mm, 0 = none)", "float", 3.5, 0.0, 20.0, 0.25),
            "hole_inset": ("Corner hole inset (mm)", "float", 6.0, 1.0, 60.0, 0.5),
            "holes_at_corners": ("Holes at 4 corners", "bool", True, None, None, None),
        },
    },
    "sketch": {
        "label": "Sketch + extrude (drawn profile)",
        "params": {
            "points": (
                "Outline points, one \"x,y\" per line (mm, at least 3)",
                "points",
                "0,0\n30,0\n30,15\n15,25\n0,15",
                None,
                None,
                None,
            ),
            "thickness": ("Extrude thickness (mm)", "float", 4.0, 0.25, 40.0, 0.25),
            "midplane": ("Extrude both ways from the plane", "bool", False, None, None, None),
            "smooth": (
                "Smooth free-form curve through the points instead of straight edges",
                "bool", False, None, None, None,
            ),
        },
    },
    "revolve": {
        "label": "Sketch + revolve (drawn profile about Z)",
        "params": {
            "points": (
                "Profile points, one \"radius,height\" per line (radius >= 0)",
                "points",
                "0,0\n8,0\n8,3\n5,3\n5,14\n0,14",
                None,
                None,
                None,
            ),
            "angle": ("Revolve angle (deg)", "float", 360.0, 5.0, 360.0, 5.0),
            "smooth": (
                "Smooth free-form curve through the points instead of straight edges",
                "bool", False, None, None, None,
            ),
        },
    },
}


def _box_snippet(var: str, p: dict) -> str:
    return f'''
    with BuildPart() as {var}_b:
        Box({p["length"]}, {p["width"]}, {p["height"]})
    {var}_base = {var}_b.part
'''


def _rod_snippet(var: str, p: dict) -> str:
    return f'''
    with BuildPart() as {var}_b:
        Cylinder({p["radius"]}, {p["length"]})
    {var}_base = {var}_b.part
'''


def _tube_snippet(var: str, p: dict) -> str:
    # inner_radius is clamped below outer_radius by _validate_feature_params'
    # cross-check, so the bore never swallows the whole wall.
    return f'''
    with BuildPart() as {var}_b:
        Cylinder({p["outer_radius"]}, {p["length"]})
        if {p["inner_radius"]} > 0:
            Cylinder({p["inner_radius"]}, {p["length"]} * 3, mode=Mode.SUBTRACT)
    {var}_base = {var}_b.part
'''


def _link_snippet(var: str, p: dict) -> str:
    # Centered on local X (-length/2 .. length/2), matching box/rod's
    # centered convention -- the feature's own position/rotation places it.
    return f'''
    {var}_length = {p["length"]}
    {var}_end_w = {p["end_width"]}
    {var}_waist_w = {p["waist_width"]}
    {var}_thickness = {p["thickness"]}
    {var}_boss_dia = {p["boss_dia"]}
    {var}_boss_h = {p["boss_height"]}
    {var}_hole_dia = {p["hole_dia"]}
    {var}_half_end = {var}_end_w / 2
    {var}_half_waist = {var}_waist_w / 2
    {var}_x0, {var}_x1 = -{var}_length / 2, {var}_length / 2
    {var}_outline = [
        ({var}_x0, {var}_half_end), (0, {var}_half_waist), ({var}_x1, {var}_half_end),
        ({var}_x1, -{var}_half_end), (0, -{var}_half_waist), ({var}_x0, -{var}_half_end),
    ]
    {var}_pivots = []
    if {p["hole_start"]}:
        {var}_pivots.append(({var}_x0, 0))
    if {p["hole_end"]}:
        {var}_pivots.append(({var}_x1, 0))
    with BuildPart() as {var}_b:
        with BuildSketch() as {var}_sk:
            Polygon(*{var}_outline)
            if {var}_pivots:
                with Locations(*{var}_pivots):
                    Circle({var}_half_end)
        extrude({var}_sk.sketch, amount={var}_thickness)
        if {var}_pivots and {var}_hole_dia > 0:
            with Locations(*[(px, py, 0) for px, py in {var}_pivots]):
                Cylinder({var}_hole_dia / 2, {var}_thickness * 3, mode=Mode.SUBTRACT)
    {var}_base = _soften({var}_b.part)
    if {var}_pivots and {var}_boss_dia > 0 and {var}_boss_h > 0:
        with BuildPart() as {var}_boss:
            with Locations(*[(px, py, {var}_thickness) for px, py in {var}_pivots]):
                Cylinder({var}_boss_dia / 2, {var}_boss_h)
            if {var}_hole_dia > 0:
                with Locations(*[(px, py, {var}_thickness) for px, py in {var}_pivots]):
                    Cylinder({var}_hole_dia / 2, {var}_boss_h * 3, mode=Mode.SUBTRACT)
        {var}_base = {var}_base + {var}_boss.part
    # Only re-center in Z: the outline is already built centered on X
    # (-length/2 .. +length/2), so shifting X here as well would offset every
    # link by half its length from where the user actually placed it.
    {var}_base = Location((0, 0, -{var}_thickness / 2)) * {var}_base
'''


def _coil_snippet(var: str, p: dict) -> str:
    # Helix ignores an ambient BuildPart(plane) context (unlike Cylinder), so
    # this is always built canonically about +Z and placed by the feature's
    # own Location transform.
    return f'''
    {var}_length = {p["length"]}
    {var}_turns = {p["turns"]}
    {var}_pitch = {var}_length / {var}_turns
    with BuildPart() as {var}_b:
        {var}_helix = Helix(pitch={var}_pitch, height={var}_length, radius={p["coil_radius"]})
        {var}_e = {var}_helix.edges()[0]
        with BuildSketch(Plane(origin={var}_e.position_at(0), z_dir={var}_e.tangent_at(0))) as {var}_sk:
            Circle({p["wire_radius"]})
        sweep({var}_sk.sketch, path={var}_helix)
    {var}_base = Location((0, 0, -{var}_length / 2)) * {var}_b.part
'''


def _plate_snippet(var: str, p: dict) -> str:
    return f'''
    {var}_length = {p["length"]}
    {var}_width = {p["width"]}
    {var}_thickness = {p["thickness"]}
    {var}_hole_dia = {p["hole_dia"]}
    {var}_hole_inset = {p["hole_inset"]}
    with BuildPart() as {var}_b:
        with BuildSketch() as {var}_sk:
            RectangleRounded({var}_length, {var}_width, radius={p["corner_radius"]})
        extrude({var}_sk.sketch, amount={var}_thickness)
        if {p["holes_at_corners"]} and {var}_hole_dia > 0:
            {var}_hx, {var}_hy = {var}_length / 2 - {var}_hole_inset, {var}_width / 2 - {var}_hole_inset
            with Locations(({var}_hx, {var}_hy, 0), (-{var}_hx, {var}_hy, 0), ({var}_hx, -{var}_hy, 0), (-{var}_hx, -{var}_hy, 0)):
                Cylinder({var}_hole_dia / 2, {var}_thickness * 3, mode=Mode.SUBTRACT)
    {var}_base = {var}_b.part
'''


def _sketch_snippet(var: str, p: dict) -> str:
    points_literal = ", ".join(f"({x}, {y})" for x, y in p["points"])
    # See shapes.polygon_source for why smooth mode is an open Spline plus one
    # straight closing Line (matching Polygon's implicit last->first edge)
    # rather than a periodic self-closing spline.
    if p["smooth"]:
        profile = (
            f"            with BuildLine() as {var}_ln:\n"
            f"                Spline(*{var}_outline)\n"
            f"                Line({var}_outline[-1], {var}_outline[0])\n"
            f"            make_face()"
        )
    else:
        profile = f"            Polygon(*{var}_outline)"
    return f'''
    {var}_outline = [{points_literal}]
    with BuildPart() as {var}_b:
        with BuildSketch() as {var}_sk:
{profile}
        extrude({var}_sk.sketch, amount={p["thickness"]}, both={p["midplane"]})
    {var}_base = {var}_b.part
'''


def _revolve_snippet(var: str, p: dict) -> str:
    # Profile lives on the XZ plane with x = radius from the Z axis; every x is
    # clamped >= 0 in validation, because a profile that crosses its own axis
    # of revolution makes a self-intersecting solid that OCC rejects. Smooth
    # mode (see shapes.polygon_source) only curves the edges BETWEEN the given
    # points -- a spline can still overshoot its control points mid-segment,
    # so a profile with points close to radius=0 can still bulge past the
    # axis even though every literal point stays clamped >= 0.
    points_literal = ", ".join(f"({x}, {y})" for x, y in p["points"])
    if p["smooth"]:
        profile = (
            f"            with BuildLine() as {var}_ln:\n"
            f"                Spline(*{var}_profile)\n"
            f"                Line({var}_profile[-1], {var}_profile[0])\n"
            f"            make_face()"
        )
    else:
        profile = f"            Polygon(*{var}_profile)"
    return f'''
    {var}_profile = [{points_literal}]
    with BuildPart() as {var}_b:
        with BuildSketch(Plane.XZ) as {var}_sk:
{profile}
        revolve(axis=Axis.Z, revolution_arc={p["angle"]})
    {var}_base = {var}_b.part
'''


SNIPPET_BUILDERS = {
    "box": _box_snippet,
    "rod": _rod_snippet,
    "tube": _tube_snippet,
    "link": _link_snippet,
    "coil": _coil_snippet,
    "plate": _plate_snippet,
    "sketch": _sketch_snippet,
    "revolve": _revolve_snippet,
}

_MODE_OPS = {"add": "+", "subtract": "-", "intersect": "&"}


def _validate_feature_params(ftype: str, raw_params: dict) -> dict:
    if ftype not in FEATURE_SCHEMA:
        raise ValueError(f"Unknown feature type '{ftype}'. Valid: {', '.join(FEATURE_SCHEMA)}")
    out = {}
    for name, (_, kind, default, lo, hi, _step) in FEATURE_SCHEMA[ftype]["params"].items():
        raw = raw_params.get(name, default)
        if kind == "bool":
            out[name] = bool(raw)
        elif kind == "points":
            out[name] = _parse_points(raw)
        else:
            value = float(raw)
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
            out[name] = value

    # Cross-param constraints that a per-field min/max can't express.
    if ftype == "tube" and out["inner_radius"] >= out["outer_radius"]:
        out["inner_radius"] = max(0.0, out["outer_radius"] - 0.25)
    if ftype == "revolve":
        # A revolve profile may not cross its own axis.
        out["points"] = [(max(0.0, x), y) for x, y in out["points"]]
    return out


def _validate_xyz(raw: dict, keys: tuple[str, str, str]) -> tuple[float, float, float]:
    raw = raw or {}
    return tuple(float(raw.get(k, 0.0)) for k in keys)  # type: ignore[return-value]


def _pattern_transforms(
    raw_pattern: dict | None,
    pos: tuple[float, float, float],
    rot: tuple[float, float, float],
) -> list[str]:
    """Return one Location-expression string per patterned instance.

    Unpatterned features get a single transform, so the pattern path and the
    plain path share exactly one code shape downstream.
    """
    px, py, pz = pos
    rx, ry, rz = rot
    plain = f"Location(({px}, {py}, {pz}), ({rx}, {ry}, {rz}))"
    if not raw_pattern:
        return [plain]

    ptype = raw_pattern.get("type", "none")
    if ptype in ("none", "", None):
        return [plain]

    count = int(float(raw_pattern.get("count", 1)))
    count = max(1, min(MAX_PATTERN_COUNT, count))
    if count == 1:
        return [plain]

    if ptype == "linear":
        dx = float(raw_pattern.get("dx", 0.0))
        dy = float(raw_pattern.get("dy", 0.0))
        dz = float(raw_pattern.get("dz", 0.0))
        return [
            f"Location(({px + dx * i}, {py + dy * i}, {pz + dz * i}), ({rx}, {ry}, {rz}))"
            for i in range(count)
        ]

    if ptype == "circular":
        cx = float(raw_pattern.get("cx", 0.0))
        cy = float(raw_pattern.get("cy", 0.0))
        cz = float(raw_pattern.get("cz", 0.0))
        total = float(raw_pattern.get("angle", 360.0))
        # A full 360 revolution shouldn't stack instance N on top of instance 0.
        step = total / count if abs(total) >= 359.999 else (total / (count - 1))
        transforms = []
        for i in range(count):
            ang = step * i
            # Rotate about the pattern center, then apply the feature's own
            # placement relative to that center -- composing as nested
            # Locations lets build123d do the matrix math rather than
            # re-deriving intrinsic Euler composition here.
            transforms.append(
                f"Location(({cx}, {cy}, {cz}), (0, 0, {ang})) * "
                f"Location(({px - cx}, {py - cy}, {pz - cz}), ({rx}, {ry}, {rz}))"
            )
        return transforms

    raise ValueError(f"Unknown pattern type '{ptype}'. Valid: none, linear, circular")


def _feature_lines(var: str, raw: dict, index_label: str) -> list[str]:
    """Emit the source lines that build one feature into `{var}` (already
    placed, already patterned). Raises ValueError on anything invalid."""
    ftype = raw.get("type")
    if ftype not in SNIPPET_BUILDERS:
        raise ValueError(
            f"Feature {index_label}: unknown type '{ftype}'. Valid: {', '.join(SNIPPET_BUILDERS)}"
        )
    params = _validate_feature_params(ftype, raw.get("params") or {})
    pos = _validate_xyz(raw.get("position"), ("x", "y", "z"))
    rot = _validate_xyz(raw.get("rotation"), ("rx", "ry", "rz"))
    transforms = _pattern_transforms(raw.get("pattern"), pos, rot)

    lines = [f"    # feature {index_label}: {ftype}", SNIPPET_BUILDERS[ftype](var, params)]
    if len(transforms) == 1:
        lines.append(f"    {var} = {transforms[0]} * {var}_base")
    else:
        # Group pattern instances with Compound, not `+`. A boolean fuse of
        # many instances is needless work, and it is actively dangerous when
        # instances happen to touch (a bolt circle whose heads just kiss, a
        # dense linear array) -- see _body_lines' comment for why a coincident-
        # face fuse can silently return an EMPTY, still "valid" shape.
        joined = ",\n        ".join(transforms)
        lines.append(f"    {var}_placements = [\n        {joined},\n    ]")
        lines.append(f"    {var} = Compound(children=[_t * {var}_base for _t in {var}_placements])")
    return lines


def _body_lines(body_var: str, features: list[dict], body_label: str) -> list[str]:
    """Fold one body's features together into `{body_var}`.

    ADD features accumulate via Compound, not a boolean fuse (`+`). This
    matters a lot for real linkage geometry: two arms of a bell-crank sharing
    one pivot boss is the single most common multi-feature pattern for a
    lever, and `+`-fusing two solids with an exactly-coincident (or, worse,
    floating-point-NEARLY-coincident after independent Location transforms)
    boss face is a known OpenCASCADE boolean-op robustness hole -- it doesn't
    raise, it just silently returns an empty, still `is_valid` shape. That
    was caught here empirically: two link features sharing a pivot fused to
    zero volume with no error at all, so the server reported `ok: True` on
    an empty STEP. A Compound of untouched solids sidesteps the whole
    problem (`.volume` still sums correctly, subtract/intersect against it
    still works -- OCC's Cut/Common accept a compound operand fine, it's
    specifically same-type Fuse-of-coincident-solids that's fragile), and
    it's the geometrically correct call anyway -- touching arms at a pivot
    don't need to be one watertight manifold, they need to occupy the space
    they occupy.

    SUBTRACT/INTERSECT still use a real boolean (`-`/`&`) -- those need true
    solid operations, and empty results there raise on their own (cutting a
    zero-volume tool is a no-op, not a silent full-body wipeout) since the
    base being cut is generally not near-coincident with the cutting tool.
    """
    live = [f for f in features if not f.get("suppressed")]
    if not live:
        raise ValueError(f"Body '{body_label}' has no active features")

    lines: list[str] = []
    add_group: list[str] = []          # vars pending Compound-merge into body_var
    started = False
    for i, raw in enumerate(live):
        var = f"{body_var}_f{i}"
        lines += _feature_lines(var, raw, f"{body_label}.{i}")
        mode = raw.get("mode", "add") if i > 0 else "add"
        if mode not in _MODE_OPS:
            raise ValueError(
                f"Feature {body_label}.{i}: mode must be add/subtract/intersect, got {mode!r}"
            )
        if mode == "add":
            add_group.append(var)
            continue
        # A subtract/intersect needs one real solid to operate against --
        # flush any pending adds into body_var first.
        if add_group:
            lines.append(_flush_add_group(body_var, add_group, started))
            add_group = []
            started = True
        lines.append(f"    {body_var} = {body_var} {_MODE_OPS[mode]} {var}")
        started = True
    if add_group:
        lines.append(_flush_add_group(body_var, add_group, started))
    return lines


def _flush_add_group(body_var: str, add_vars: list[str], started: bool) -> str:
    # _flat() flattens one level so this never nests a Compound inside a
    # Compound: build123d's `.volume` only sums DIRECT children, so
    # Compound(children=[Compound(...), x]) reads volume=0 even though
    # .solids() still (correctly) lists every solid -- a silent, `is_valid`
    # -true, wrong-answer trap. body_var may already be a Compound from an
    # earlier flush, and any add var may already be one from a patterned
    # feature, so both sides need flattening, not just one.
    names = add_vars if not started else [body_var, *add_vars]
    joined = " + ".join(f"_flat({n})" for n in names)
    return f"    {body_var} = Compound(children={joined})"


def _flat_helper_source() -> str:
    # NOT an isinstance(x, Compound) check -- build123d's own `Part`/`Solid`
    # classes are themselves Compound subclasses (isinstance is True for a
    # single leaf solid too), and a leaf's `.children` is an empty list, not
    # itself. That combination silently emptied every single-feature body
    # the first time this helper was tried (Compound(children=[]) from a
    # leaf misidentified as an already-flat container). `.solids()` has no
    # such trap: it recurses to the real leaf solids regardless of nesting
    # depth, correctly returning `[x]` for a leaf and the full flat list for
    # any depth of Compound-of-Compounds.
    return '''
def _flat(x):
    return list(x.solids())
'''


_PREAMBLE = '''
from build123d import (
    BuildPart, BuildSketch, BuildLine, Box, Cylinder, Polygon, Spline, Line,
    make_face, Circle, RectangleRounded, Locations, Location, Plane, extrude,
    revolve, Mode, Helix, sweep, Axis, fillet, Compound,
)
'''


def _module(body: str) -> str:
    return f"{_PREAMBLE}{_soften_helper_source()}{_flat_helper_source()}\n\ndef gen_step():\n{body}\n"


def compose_source(raw_features: list[dict]) -> str:
    """Single-body source (the original flat-feature-list contract)."""
    if not raw_features:
        raise ValueError("At least one feature is required")
    if len(raw_features) > MAX_FEATURES:
        raise ValueError(f"Too many features ({len(raw_features)}), max {MAX_FEATURES}")
    lines = _body_lines("result", raw_features, "0")
    return _module("\n".join(lines) + "\n    return result")


def compose_body_source(body: dict) -> str:
    """Source for one named body of a multi-body model."""
    features = body.get("features") or []
    if len(features) > MAX_FEATURES:
        raise ValueError(f"Too many features ({len(features)}), max {MAX_FEATURES}")
    lines = _body_lines("result", features, str(body.get("name", "body")))
    return _module("\n".join(lines) + "\n    return result")


def compose_multibody_runner(bodies: list[dict], assembly_name: str) -> tuple[str, list[str]]:
    """One self-contained script that builds EVERY body and exports them all.

    Why this exists instead of calling the CAD skill's `scripts/step` once per
    body: nearly all of a single run's wall time is interpreter + OpenCASCADE
    import, not geometry (~8s per call on the dyno, of which the actual solid
    is a fraction). Paying that per body meant a 4-body model blew Heroku's
    30s router timeout. Building every body in one process pays the import
    cost once.

    Exports each body to `<safe_name>.step`/`.stl` individually and the whole
    model to `<assembly_name>.step`. Only individual solids are meshed to STL
    -- asking OCC for an STL of a multi-solid Compound can crash it outright.

    Returns (source, safe_names) where safe_names[i] is the on-disk basename
    used for bodies[i].
    """
    if not bodies:
        raise ValueError("At least one body is required")
    if len(bodies) > MAX_BODIES:
        raise ValueError(f"Too many bodies ({len(bodies)}), max {MAX_BODIES}")
    total = sum(len(b.get("features") or []) for b in bodies)
    if total > MAX_FEATURES:
        raise ValueError(f"Too many features across all bodies ({total}), max {MAX_FEATURES}")

    lines: list[str] = []
    body_vars: list[str] = []
    safe_names: list[str] = []
    seen: set[str] = set()

    for bi, raw_body in enumerate(bodies):
        features = [f for f in (raw_body.get("features") or []) if not f.get("suppressed")]
        if not features:
            raise ValueError(f"Body '{raw_body.get('name', bi)}' has no active features")
        bvar = f"b{bi}"
        lines += _body_lines(bvar, features, str(raw_body.get("name", f"body{bi}")))
        body_vars.append(bvar)

        base = "".join(c for c in str(raw_body.get("name") or f"body{bi}") if c.isalnum() or c in "_-") or f"body{bi}"
        name = base
        n = 2
        while name in seen:                      # two bodies may share a display name
            name = f"{base}_{n}"
            n += 1
        seen.add(name)
        safe_names.append(name)

    body = "\n".join(lines)
    exports = "\n".join(
        f"    _export_one({var}, {name!r})" for var, name in zip(body_vars, safe_names)
    )
    asm = "".join(c for c in assembly_name if c.isalnum() or c in "_-") or "assembly"

    source = f'''{_PREAMBLE}
import json, traceback
from build123d import export_step, export_stl
{_soften_helper_source()}{_flat_helper_source()}

_manifest = {{"bodies": [], "assembly": None}}

def _export_one(part, name):
    entry = {{"name": name, "step": False, "stl": False, "error": None}}
    try:
        export_step(part, name + ".step")
        entry["step"] = True
    except Exception as exc:
        entry["error"] = "step export failed: {{}}".format(exc)
    try:
        # Coarser than export_stl's 1e-3mm default on purpose: this mesh only
        # feeds the browser viewer, and the default tessellation of a swept
        # coil ran to ~8MB (over 10MB once base64'd into the JSON response).
        # The STEP alongside it is the precision deliverable.
        export_stl(part, name + ".stl", tolerance=0.02, angular_tolerance=0.25)
        entry["stl"] = True
    except Exception as exc:
        # A missing STL is survivable -- the STEP is the real deliverable and
        # the viewer just won't be able to show this body.
        if entry["error"] is None:
            entry["error"] = "stl export failed: {{}}".format(exc)
    _manifest["bodies"].append(entry)

def build():
{body}
{exports}
    try:
        export_step(Compound(children=[c for v in [{", ".join(body_vars)}] for c in _flat(v)]), "{asm}.step")
        _manifest["assembly"] = "{asm}.step"
    except Exception as exc:
        _manifest["assembly_error"] = str(exc)

try:
    build()
except Exception:
    _manifest["fatal"] = traceback.format_exc()

print("__CADMCP_MANIFEST__" + json.dumps(_manifest))
'''
    return source, safe_names


def compose_assembly_source(bodies: list[dict]) -> str:
    """Source for the whole model as one Compound of every body.

    Note: a multi-solid Compound exports to STEP fine, but asking the CAD
    skill's CLI for an STL sidecar of one can crash the OCC side silently --
    so callers should request this with stl=False and mesh each body
    separately for preview (see the /api/generate-assembly handler).
    """
    if not bodies:
        raise ValueError("At least one body is required")
    if len(bodies) > MAX_BODIES:
        raise ValueError(f"Too many bodies ({len(bodies)}), max {MAX_BODIES}")
    total = sum(len(b.get("features") or []) for b in bodies)
    if total > MAX_FEATURES:
        raise ValueError(f"Too many features across all bodies ({total}), max {MAX_FEATURES}")

    lines: list[str] = []
    body_vars: list[str] = []
    for bi, body in enumerate(bodies):
        if body.get("suppressed"):
            continue
        features = [f for f in (body.get("features") or []) if not f.get("suppressed")]
        if not features:
            continue
        bvar = f"b{bi}"
        lines += _body_lines(bvar, features, str(body.get("name", f"body{bi}")))
        body_vars.append(bvar)

    if not body_vars:
        raise ValueError("Every body is empty or suppressed -- nothing to build")

    lines.append(f"    result = Compound(children=[c for v in [{', '.join(body_vars)}] for c in _flat(v)])")
    return _module("\n".join(lines) + "\n    return result")
