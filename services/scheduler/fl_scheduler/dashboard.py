"""Serve the built dashboard on explicit public UI paths; API paths never get SPA fallback."""

from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_dashboard(app: FastAPI, directory: Path) -> None:
    root = directory.resolve()
    index = root / "index.html"
    if not index.is_file() or not (root / "assets").is_dir():
        raise ValueError("Build the dashboard before setting FL_DASHBOARD_DIR")
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="dashboard-assets")

    def page() -> FileResponse:
        response = FileResponse(index)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    for path in ("/", "/clusters", "/jobs", "/admin/clusters", "/admin/users"):
        app.add_api_route(path, page, methods=["GET", "HEAD"], include_in_schema=False)

    @app.get("/clusters/{cluster_id}", include_in_schema=False)
    def cluster(cluster_id: UUID) -> FileResponse:
        return page()

    @app.get("/jobs/{job_id}", include_in_schema=False)
    def job(job_id: UUID) -> FileResponse:
        return page()

    @app.get("/boards/{board_id}", include_in_schema=False)
    def board(board_id: str) -> FileResponse:
        from re import fullmatch

        if fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", board_id) is None:
            raise HTTPException(404)
        return page()
