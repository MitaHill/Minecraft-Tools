from asyncio import CancelledError, Event, create_task
from contextlib import asynccontextmanager, suppress
from os import environ
from pathlib import Path
from re import fullmatch
from tempfile import gettempdir
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .admin import Administration
from .admin import router as admin_router
from .client_api import router as client_router
from .database import diagnostics, open_database
from .lobby import LobbySettings, cleanup_lobby
from .lobby import router as lobby_router
from .node_hub import NodeHub, server_tls
from .rate_limit import RateLimit
from .scanner import ScanPolicy, run_scanner

ReportLabel = Annotated[str, StringConstraints(min_length=1, max_length=128)]
LogLine = Annotated[str, StringConstraints(max_length=301)]


class DiagnosticReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    created_at: int = Field(ge=0)
    launcher_version: ReportLabel
    os: ReportLabel
    os_version: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    arch: ReportLabel
    channel: Literal["dev", "stable", "-"]
    build_channel: Literal["dev", "stable", "-"]
    version: ReportLabel
    stage: ReportLabel
    exit_code: int | None
    log: list[LogLine] = Field(max_length=40)


@asynccontextmanager
async def lifespan(app: FastAPI):
    default_build_dir = Path(gettempdir()) / "minecrafttools-build"
    build_dir = Path(environ.get("MINECRAFTTOOLS_BUILD_ROOT", default_build_dir))
    data_dir = Path(environ.get("CONTROL_DATA_DIR", build_dir / "control-data"))
    app.state.lobby_settings = LobbySettings.load(
        environ.get("CONTROL_LOBBY_TARGETS", ""),
        environ.get("CONTROL_LOBBY_BLOCKWORDS_FILE", ""),
    )
    app.state.administration = Administration.load(
        environ.get("CONTROL_ADMIN_USERS_FILE", ""),
        secure_cookie=environ.get("CONTROL_ADMIN_COOKIE_SECURE", "1") != "0",
    )
    database = open_database(data_dir)
    app.state.database = database
    app.state.release_dir = Path(
        environ.get("CONTROL_RELEASE_DIR", data_dir / "releases")
    )
    app.state.install_rate_limit = RateLimit(10, 300)
    app.state.claim_rate_limit = RateLimit(30, 60)
    cleanup = create_task(cleanup_lobby(database))
    hub = None
    scanner = None
    stop_scanner = Event()
    app.state.scanner_enabled = environ.get("CONTROL_SCANNER_ENABLED", "0") == "1"
    try:
        port = int(environ.get("CONTROL_NODE_HUB_PORT", "0"))
        if port:
            hub = NodeHub(database)
            await hub.start(
                environ.get("CONTROL_NODE_HUB_HOST", "127.0.0.1"),
                port,
                server_tls(
                    environ["CONTROL_NODE_HUB_CERT"], environ["CONTROL_NODE_HUB_KEY"]
                ),
            )
        app.state.node_hub = hub
        if app.state.scanner_enabled:
            if hub is None:
                raise ValueError("The tunnel scanner requires the node hub")
            scanner = create_task(run_scanner(database, ScanPolicy(), stop_scanner))
        yield
    finally:
        if scanner is not None:
            stop_scanner.set()
            await scanner
        cleanup.cancel()
        with suppress(CancelledError):
            await cleanup
        if hub is not None:
            await hub.close()
        database.dispose()


app = FastAPI(title="MinecraftTools Control Server", lifespan=lifespan)
app.include_router(lobby_router, prefix="/api")
app.include_router(lobby_router, prefix="/api/v1")
app.include_router(admin_router)
app.include_router(client_router)

admin_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
if (admin_dist / "index.html").is_file():
    app.frontend("/admin", directory=admin_dist)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/releases/{channel}.json")
def releases(channel: Literal["dev", "stable"], request: Request):
    path = request.app.state.release_dir / f"{channel}.json"
    try:
        with path.open("rb") as stream:
            body = stream.read(512 * 1024 + 1)
    except OSError:
        raise HTTPException(503, "Signed release manifest is unavailable") from None
    if not body or len(body) > 512 * 1024:
        raise HTTPException(503, "Signed release manifest is unavailable")
    return Response(
        body, media_type="application/json", headers={"Cache-Control": "no-cache"}
    )


@app.get("/releases/artifacts/{filename}")
def release_artifact(filename: str, request: Request):
    if not fullmatch(
        r"MinecraftTools-update-\d{4}-\d{2}-\d{2}-[1-9]\d*-(windows|darwin|linux)-(amd64|arm64)\.tar\.gz",
        filename,
    ):
        raise HTTPException(404, "Release artifact not found")
    path = request.app.state.release_dir / "artifacts" / filename
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "Release artifact not found")
    return FileResponse(path, media_type="application/gzip", filename=filename)


@app.post("/diagnostics", status_code=status.HTTP_201_CREATED, response_class=Response)
def receive_diagnostics(report: DiagnosticReport, request: Request) -> Response:
    os_name = {"macos": "darwin"}.get(report.os, report.os)
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(report.arch, report.arch)
    # FastAPI runs this handler in its thread pool. Acknowledge after the commit.
    with request.app.state.database.begin() as connection:
        connection.execute(
            diagnostics.insert().values(
                os=os_name, arch=arch, report=report.model_dump()
            )
        )
    return Response(status_code=status.HTTP_201_CREATED)
