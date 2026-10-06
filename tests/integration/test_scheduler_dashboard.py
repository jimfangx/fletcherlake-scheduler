"""Browser build acceptance runs explicitly with a real HTTPS scheduler/PostgreSQL."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.dashboard_server import browser_server


def test_react_dashboard_browser_acceptance(auth_stack, scheduler_db, config, tmp_path):
    browsers = os.environ.get("FL_TEST_DASHBOARD_BROWSER")
    if not browsers:
        pytest.skip(
            "Set FL_TEST_DASHBOARD_BROWSER after building the dashboard/installing Chromium"
        )
    dashboard = Path(__file__).resolve().parents[2] / "services" / "dashboard"
    assert (dashboard / "dist" / "index.html").is_file(), (
        "Run pixi web-build before browser acceptance"
    )
    with browser_server(auth_stack, scheduler_db, config, tmp_path, dashboard / "dist") as fixture:
        env = {
            **os.environ,
            "FL_BROWSER_FIXTURE": str(fixture),
            "PLAYWRIGHT_BROWSERS_PATH": browsers,
        }
        result = subprocess.run(
            ["pixi", "run", "-e", "web", "npm", "run", "test:browser"],
            cwd=dashboard,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
