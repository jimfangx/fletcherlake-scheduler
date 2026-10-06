"""Overview counts use replicated state without leaking other owners' queued job IDs."""

from fastapi.testclient import TestClient
from fl_common.models.scheduler import Principal
from fl_scheduler.agents.reconcile import Reconciler
from fl_scheduler.api.queries import Queries
from fl_scheduler.dashboard import mount_dashboard
from fl_scheduler.registry import Registry

from tests.dashboard_snapshot import snapshot


def test_counts_and_board_identity_preserve_owner_boundaries(scheduler_db, config):
    registered = Registry(scheduler_db).register(config, "agent")
    alice = Principal(email="alice@example.edu", subject="alice", role="user")
    queries = Queries(scheduler_db)
    assert queries.cluster(registered.cluster_id, alice)["queued_count"] is None
    data = snapshot(registered)
    data.jobs[0].spec.owner = "bob@example.edu"
    data.jobs[1].spec.owner = alice.email
    reconciler = Reconciler(scheduler_db)
    session = reconciler.connected(registered.cluster_id)
    reconciler.snapshot(registered.cluster_id, session, data)
    view = queries.cluster(registered.cluster_id, alice)
    assert view["running_count"] == 1 and view["queued_count"] == 1
    assert view["snapshot_at"] == data.model_dump(mode="json")["timestamp"]
    board = queries.board("board-0", alice)
    assert board["cluster_id"] == str(registered.cluster_id) and board["queued_count"] == 1
    assert board["active_job"] is None
    assert str(data.jobs[0].spec.job_id) not in str(view)
    assert str(data.jobs[1].spec.job_id) not in str(view)  # Queue projection exposes counts only.


def test_spa_fallback_is_limited_to_ui_paths_and_assets(auth_stack, tmp_path):
    _, _, _, _, app = auth_stack
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>dashboard shell</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log('fixture')")
    (tmp_path / "private.txt").write_text("never published")
    mount_dashboard(app, tmp_path)
    with TestClient(app, base_url="https://scheduler.test") as client:
        for path in (
            "/",
            "/clusters",
            "/jobs",
            "/admin/clusters",
            "/admin/users",
            "/boards/board-0",
        ):
            response = client.get(path)
            assert response.status_code == 200 and "dashboard shell" in response.text
            assert "script-src 'self'" in response.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        for path in (
            "/api/unknown",
            "/private.txt",
            "/assets/../private.txt",
            "/unknown",
            "/clusters/not-a-uuid",
        ):
            assert client.get(path).status_code in {404, 422}
        assert client.get("/api/clusters").status_code == 401
