"""Feature-tree composer: an ordered list of positioned primitives combined
with ADD/SUBTRACT/INTERSECT into one part -- the "flexibility" layer on top
of shapes.py's single-shape templates.

Each feature is:
    {"type": "box"|"rod"|"link"|"coil"|"plate"|"sketch",
     "params": {...type-specific, see FEATURE_SCHEMA...},
     "position": {"x":.., "y":.., "z":..},
     "rotation": {"rx":.., "ry":.., "rz":..},   # degrees, build123d's
                                                  # intrinsic order (rx about
                                                  # world X, then ry about
                                                  # the new Y, then rz about
                                                  # the newest Z -- matters
                                                  # once more than one axis
                                                  # is non-zero)
     "mode": "add"|"subtract"|"intersect"}       # feature 0 is always "add"

Every primitive is built in its own LOCAL frame (centered on its own axis
for box/rod/link, base-anchored at z=0 for plate/sketch -- whichever is the
natural convention for that shape), then positioned via a single
Location(position, rotation) transform, then folded into the running result
with the requested boolean op. As with shapes.py, every value here is
already validated to be a plain float/bool/point-list before it is ever
formatted into source -- no request text is interpolated as code.
"""

from __future__ import annotations

from shapes import _parse_points, _soften_helper_source

MAX_FEATURES = 25

# Per-type param schema: (label, kind, default, min, max, step). Position,
# rotation, and mode are handled generically for every type, not listed here.
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
        "label": "Sketch + extrude (drawn polygon)",
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
        },
    },
}


def _box_snippet(var: str, p: dict) -> str:
    return f'''
    with BuildPart() as {var}_b:
        Box({p["length"]}, {p["width"]}, {p["height"]})
    {var} = {var}_b.part
'''


def _rod_snippet(var: str, p: dict) -> str:
    return f'''
    with BuildPart() as {var}_b:
        Cylinder({p["radius"]}, {p["length"]})
    {var} = {var}_b.part
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
    {var} = _soften({var}_b.part)
    if {var}_pivots and {var}_boss_dia > 0 and {var}_boss_h > 0:
        with BuildPart() as {var}_boss:
            with Locations(*[(px, py, {var}_thickness) for px, py in {var}_pivots]):
                Cylinder({var}_boss_dia / 2, {var}_boss_h)
            if {var}_hole_dia > 0:
                with Locations(*[(px, py, {var}_thickness) for px, py in {var}_pivots]):
                    Cylinder({var}_hole_dia / 2, {var}_boss_h * 3, mode=Mode.SUBTRACT)
        {var} = {var} + {var}_boss.part
    {var} = Location((-{var}_length / 2, 0, -{var}_thickness / 2)) * {var}
'''


def _coil_snippet(var: str, p: dict) -> str:
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
    {var} = Location((0, 0, -{var}_length / 2)) * {var}_b.part
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
    {var} = {var}_b.part
'''


def _sketch_snippet(var: str, p: dict) -> str:
    points_literal = ", ".join(f"({x}, {y})" for x, y in p["points"])
    return f'''
    {var}_outline = [{points_literal}]
    with BuildPart() as {var}_b:
        with BuildSketch() as {var}_sk:
            Polygon(*{var}_outline)
        extrude({var}_sk.sketch, amount={p["thickness"]})
    {var} = {var}_b.part
'''


SNIPPET_BUILDERS = {
    "box": _box_snippet,
    "rod": _rod_snippet,
    "link": _link_snippet,
    "coil": _coil_snippet,
    "plate": _plate_snippet,
    "sketch": _sketch_snippet,
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
    return out


def _validate_xyz(raw: dict, keys: tuple[str, str, str]) -> tuple[float, float, float]:
    raw = raw or {}
    return tuple(float(raw.get(k, 0.0)) for k in keys)  # type: ignore[return-value]


def compose_source(raw_features: list[dict]) -> str:
    if not raw_features:
        raise ValueError("At least one feature is required")
    if len(raw_features) > MAX_FEATURES:
        raise ValueError(f"Too many features ({len(raw_features)}), max {MAX_FEATURES}")

    body_lines: list[str] = []
    for i, raw in enumerate(raw_features):
        ftype = raw.get("type")
        if ftype not in SNIPPET_BUILDERS:
            raise ValueError(f"Feature {i}: unknown type '{ftype}'. Valid: {', '.join(SNIPPET_BUILDERS)}")
        params = _validate_feature_params(ftype, raw.get("params") or {})
        px, py, pz = _validate_xyz(raw.get("position"), ("x", "y", "z"))
        rx, ry, rz = _validate_xyz(raw.get("rotation"), ("rx", "ry", "rz"))
        mode = raw.get("mode", "add") if i > 0 else "add"
        if mode not in _MODE_OPS:
            raise ValueError(f"Feature {i}: mode must be add/subtract/intersect, got {mode!r}")

        var = f"f{i}"
        body_lines.append(f"    # feature {i}: {ftype} ({mode})")
        body_lines.append(SNIPPET_BUILDERS[ftype](var, params))
        body_lines.append(f"    {var} = Location(({px}, {py}, {pz}), ({rx}, {ry}, {rz})) * {var}")
        if i == 0:
            body_lines.append(f"    result = {var}")
        else:
            body_lines.append(f"    result = result {_MODE_OPS[mode]} {var}")

    body = "\n".join(body_lines)
    return f'''
from build123d import (
    BuildPart, BuildSketch, Box, Cylinder, Polygon, Circle, RectangleRounded,
    Locations, Location, Plane, extrude, Mode, Helix, sweep, Axis, fillet,
)
{_soften_helper_source()}

def gen_step():
{body}
    return result
'''
