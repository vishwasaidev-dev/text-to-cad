from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

import features
import shapes

REPO_ROOT = Path(__file__).resolve().parent.parent
STEP_SKILL_DIR = REPO_ROOT / "skills" / "cad" / "scripts" / "step"
INSPECT_SKILL_DIR = REPO_ROOT / "skills" / "cad" / "scripts" / "inspect"
SUBPROCESS_TIMEOUT_SECONDS = 90
OUTPUT_TAIL_CHARS = 8000

_UI_HTML = (
    (Path(__file__).resolve().parent / "ui.html")
    .read_text(encoding="utf-8")
    .replace("__SCHEMA_JSON__", json.dumps(shapes.SCHEMA))
)

_BUILDER_HTML = (
    (Path(__file__).resolve().parent / "builder.html")
    .read_text(encoding="utf-8")
    .replace("__FEATURE_SCHEMA_JSON__", json.dumps(features.FEATURE_SCHEMA))
)

# FastMCP defaults to DNS-rebinding Host-header checks scoped to localhost, which
# 421s every request once this runs behind a real hostname (Heroku). That
# protection targets browser-based attacks against locally-bound dev servers;
# it's not relevant here since every request already has to pass the Bearer
# auth check below and nothing renders this service in a browser.
mcp = FastMCP(
    "cad-mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _safe_name(name: str) -> str:
    cleaned = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    return cleaned or "part"


def _run_skill_cli(skill_dir: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(skill_dir), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


def _tail(text: str) -> str:
    return text[-OUTPUT_TAIL_CHARS:]


def _generate_step_impl(
    source: str,
    filename: str = "part",
    stl: bool = False,
    glb: bool = False,
    three_mf: bool = False,
) -> dict[str, Any]:
    """Compile build123d Python source into a STEP file via the text-to-cad CAD skill's
    `scripts/step` generator. Shared by the generate_step MCP tool and the
    /api/generate-shape REST endpoint -- same compile path either way, they
    only differ in how `source` gets built (raw from an MCP client that
    already knows build123d vs. from shapes.py's fixed templates).
    """
    name = _safe_name(filename)
    with tempfile.TemporaryDirectory(prefix="cadmcp-") as tmp:
        work_dir = Path(tmp)
        source_path = work_dir / f"{name}.py"
        source_path.write_text(source, encoding="utf-8")
        output_path = work_dir / f"{name}.step"

        # scripts/step validates path arguments and rejects Windows-style backslashes.
        args = [source_path.as_posix(), "-o", output_path.as_posix()]
        if stl:
            args += ["--stl", f"{name}.stl"]
        if glb:
            args += ["--glb", f"{name}.glb"]
        if three_mf:
            args += ["--3mf", f"{name}.3mf"]

        try:
            result = _run_skill_cli(STEP_SKILL_DIR, args, cwd=work_dir)
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error": f"generate_step timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
                "stdout": _tail(exc.stdout or ""),
                "stderr": _tail(exc.stderr or ""),
            }

        response: dict[str, Any] = {
            "ok": result.returncode == 0,
            "stdout": _tail(result.stdout),
            "stderr": _tail(result.stderr),
        }
        for key, path in (
            ("step_base64", output_path),
            ("stl_base64", work_dir / f"{name}.stl"),
            ("glb_base64", work_dir / f"{name}.glb"),
            ("three_mf_base64", work_dir / f"{name}.3mf"),
        ):
            if path.exists():
                response[key] = base64.b64encode(path.read_bytes()).decode("ascii")
        return response


@mcp.tool()
def generate_step(
    source: str,
    filename: str = "part",
    stl: bool = False,
    glb: bool = False,
    three_mf: bool = False,
) -> dict[str, Any]:
    """Compile build123d Python source into a STEP file via the text-to-cad CAD skill's
    `scripts/step` generator.

    `source` must be a complete build123d Python module that defines `gen_step()`,
    per the CAD skill conventions (units mm, closed positive-volume solids). Returns
    the generated files base64-encoded. Set `stl`/`glb`/`three_mf` to also export those
    secondary sidecars alongside the STEP.
    """
    return _generate_step_impl(source, filename, stl, glb, three_mf)


@mcp.tool()
def inspect_step(
    step_base64: str,
    filename: str = "part",
    facts: bool = True,
    planes: bool = False,
    positioning: bool = False,
    topology: bool = False,
) -> dict[str, Any]:
    """Run the text-to-cad CAD skill's `scripts/inspect refs` validation against a STEP file.

    `step_base64` is the base64-encoded contents of a .step file (e.g. the `step_base64`
    returned by `generate_step`). Returns the CLI's JSON report (facts/planes/positioning),
    e.g. bounding box, volume, and named planar faces, for validating a generated part
    against its brief.
    """
    name = _safe_name(filename)
    with tempfile.TemporaryDirectory(prefix="cadmcp-") as tmp:
        work_dir = Path(tmp)
        step_filename = f"{name}.step"
        (work_dir / step_filename).write_bytes(base64.b64decode(step_base64))

        # Pass a cwd-relative filename, not an absolute path. cadpy's cad-path
        # normalization strips leading path separators for its selector-ref catalog
        # (normalize_cad_path -> .strip("/")), which is harmless for Windows-style
        # "C:/..." paths but turns a POSIX absolute path like "/tmp/x/cube.step" into
        # a bogus relative-looking string, breaking file resolution on Linux. A plain
        # relative filename resolved against cwd (set below) sidesteps this entirely
        # and matches the CAD skill's own documented usage.
        args = ["refs", step_filename]
        if facts:
            args.append("--facts")
        if planes:
            args.append("--planes")
        if positioning:
            args.append("--positioning")
        if topology:
            args.append("--topology")

        try:
            result = _run_skill_cli(INSPECT_SKILL_DIR, args, cwd=work_dir)
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error": f"inspect_step timed out after {SUBPROCESS_TIMEOUT_SECONDS}s",
                "stdout": _tail(exc.stdout or ""),
                "stderr": _tail(exc.stderr or ""),
            }

        return {
            "ok": result.returncode == 0,
            "stdout": _tail(result.stdout),
            "stderr": _tail(result.stderr),
        }


def _is_authorized(request: Request) -> bool:
    expected = os.environ.get("MCP_AUTH_TOKEN")
    if not expected:
        # No token configured: fail closed rather than serving an open code-execution endpoint.
        return False
    return request.headers.get("authorization") == f"Bearer {expected}"


# /ui must be reachable by a plain browser navigation, which can't set a
# custom Authorization header -- the page itself prompts for the token and
# sends it on subsequent fetch() calls to /api/generate-shape, which DOES
# stay behind the auth check like every other route.
PUBLIC_PATHS = {"/", "/ui", "/ui/builder"}


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        if not _is_authorized(request):
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cad-mcp"})


async def ui_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_UI_HTML)


async def builder_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_BUILDER_HTML)


async def generate_assembly_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    raw_features = body.get("features") or []
    filename = body.get("filename") or "assembly"
    want_stl = bool(body.get("stl", True))

    try:
        source = features.compose_source(raw_features)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 - report to the UI rather than 500
        return JSONResponse({"ok": False, "error": f"unexpected error building source: {exc}"}, status_code=400)

    result = _generate_step_impl(source, filename=filename, stl=want_stl)
    return JSONResponse(result)


async def generate_shape_endpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    shape = body.get("shape")
    raw_params = body.get("params") or {}
    filename = body.get("filename") or "part"
    want_stl = bool(body.get("stl", True))

    try:
        source = shapes.build_source(shape, raw_params)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 - report to the UI rather than 500
        return JSONResponse({"ok": False, "error": f"unexpected error building source: {exc}"}, status_code=400)

    result = _generate_step_impl(source, filename=filename, stl=want_stl)
    return JSONResponse(result)


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app()
    app = Starlette(
        routes=[
            Route("/", health),
            Route("/ui", ui_page),
            Route("/ui/builder", builder_page),
            Route("/api/generate-shape", generate_shape_endpoint, methods=["POST"]),
            Route("/api/generate-assembly", generate_assembly_endpoint, methods=["POST"]),
        ],
        middleware=[Middleware(BearerAuthMiddleware)],
        # FastMCP's streamable_http_app() carries its own lifespan (starts the
        # session manager's task group). Mounting it below does NOT run that
        # lifespan automatically, so it must be forwarded explicitly or every
        # /mcp request 500s with "Task group is not initialized".
        lifespan=mcp_app.router.lifespan_context,
    )
    app.mount("/", mcp_app)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    # Force a single worker: Heroku's Python buildpack sets WEB_CONCURRENCY from
    # detected memory/cores, and uvicorn auto-reads it, but passing >1 workers to
    # uvicorn.run() requires the app as an import string (it crashes here otherwise).
    # One worker is also the right fit for this service: each request already
    # delegates the heavy build123d/OpenCASCADE work to its own subprocess, so extra
    # worker processes would only multiply resident memory for no throughput gain.
    uvicorn.run(build_app(), host="0.0.0.0", port=port, workers=1)


if __name__ == "__main__":
    main()
