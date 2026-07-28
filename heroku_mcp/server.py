from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

REPO_ROOT = Path(__file__).resolve().parent.parent
STEP_SKILL_DIR = REPO_ROOT / "skills" / "cad" / "scripts" / "step"
INSPECT_SKILL_DIR = REPO_ROOT / "skills" / "cad" / "scripts" / "inspect"
SUBPROCESS_TIMEOUT_SECONDS = 90
OUTPUT_TAIL_CHARS = 8000

mcp = FastMCP("cad-mcp")


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
        step_path = work_dir / f"{name}.step"
        step_path.write_bytes(base64.b64decode(step_base64))

        args = ["refs", step_path.as_posix()]
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


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path == "/":
            return await call_next(request)
        if not _is_authorized(request):
            return PlainTextResponse("Unauthorized", status_code=401)
        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "cad-mcp"})


def build_app() -> Starlette:
    mcp_app = mcp.streamable_http_app()
    app = Starlette(
        routes=[Route("/", health)],
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
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
