"""The two terminal-interrupt routes (§17, the emergency exit).

``terminal.run`` starts a subprocess the user has no other way to stop, and the
whole point of these routes is that a Stop press in the UI is honoured
*without* a permission prompt -- "an interrupt that first asks 'may I
interrupt?' is not an interrupt". Anything that starts a command is gated; the
interrupt itself is not, and the security position here is that the routes
expose only ``terminal.running()``, ``terminal.cancel`` and ``terminal.cancel_all``,
which by construction touch nothing but commands this process already started.

The claim worth asserting is the one the UI actually depends on: that a POST really
stopped the real process. So a command is registered in ``terminal._RUNNING`` the same
way ``run()`` registers it, then cancelled *through the route*, and the stop is verified
from ``proc.wait()`` -- the kernel's return code, ``-15`` for SIGTERM -- not from the
route's JSON, which would be asserting the thing under test against itself.

Two refusal shapes are pinned because they are distinct real paths, not one:

* A command that *finished* has already been popped from ``_RUNNING`` by ``run()``'s
  ``finally``. Cancelling it is a 200 with ``stopped: false`` and the reason -- the
  double-clicked-Stop case, and the only path where the route adds the ``run_id`` echo.
* A command still *registered* whose process has since died reports ``stopped: true``
  with ``already gone`` -- the tool tried, and honestly says there was nothing left to
  signal.

``_RUNNING`` is process-global, so an autouse fixture empties it around every test and
kills whatever a test leaves behind: one test's leaked ``sleep`` must not be counted by
the next test's ``cancel_all``.
"""
from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.api.routes import tools_routes
from nexus.tools import terminal


@pytest.fixture(autouse=True)
def isolate_running() -> Iterator[None]:
    """Every test starts with an empty ``_RUNNING`` and leaves it empty.

    The registry is a module global shared with the real tool; without this,
    a ``sleep`` one test forgot to reap is a phantom the next test's listing or
    ``cancel_all`` count would include.
    """
    saved = dict(terminal._RUNNING)
    terminal._RUNNING.clear()
    try:
        yield
    finally:
        for proc in terminal._RUNNING.values():
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        terminal._RUNNING.clear()
        terminal._RUNNING.update(saved)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(tools_routes.router)
    return TestClient(app)


def _spawn_sleeper() -> tuple[subprocess.Popen[bytes], str]:
    """A real `sleep` registered exactly as `terminal.run` registers it.

    `run()` publishes into `_RUNNING` under a uuid hex id and starts a new
    session; reproducing that here means the route under test is doing the
    cancelling, and nothing in the assertion path is a fake.
    """
    run_id = uuid.uuid4().hex[:12]
    proc = subprocess.Popen(
        ["sleep", "300"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    terminal._RUNNING[run_id] = proc
    return proc, run_id


def _reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_cancel_stops_the_real_process(client: TestClient) -> None:
    proc, run_id = _spawn_sleeper()
    try:
        listed = client.get("/api/tools/terminal/running").json()
        assert {"run_id": run_id, "pid": proc.pid} in listed["running"]

        resp = client.post("/api/tools/terminal/cancel", json={"run_id": run_id})
        body = resp.json()
        assert resp.status_code == 200
        assert body["stopped"] is True
        assert "SIGTERM" in body["detail"]

        # The kernel's word, not the route's: sleep dies with -15.
        assert proc.wait(timeout=5) == -15
        # And the bookkeeping agreed once run()'s finally would have popped it;
        # the listing filters by liveness, so the dead proc is already absent.
        assert client.get("/api/tools/terminal/running").json()["running"] == []
    finally:
        _reap(proc)


def test_cancel_a_finished_command_is_a_200_saying_it_is_not_running(client: TestClient) -> None:
    """The double-clicked-Stop case: the command already finished and was
    popped from `_RUNNING`, so the route echoes the id and the reason."""
    proc, run_id = _spawn_sleeper()
    try:
        proc.terminate()
        assert proc.wait(timeout=5) == -15
        # run()'s finally is what removes a finished command; do that here.
        terminal._RUNNING.pop(run_id, None)

        resp = client.post("/api/tools/terminal/cancel", json={"run_id": run_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["stopped"] is False
        assert body["run_id"] == run_id
        assert "not running" in body["detail"].lower()
    finally:
        _reap(proc)


def test_cancel_a_dead_but_registered_command_says_already_gone(client: TestClient) -> None:
    """A process that died while still registered: the tool tried to signal it
    and honestly reports there was nothing left to stop."""
    proc, run_id = _spawn_sleeper()
    try:
        proc.terminate()
        assert proc.wait(timeout=5) == -15
        # Left in _RUNNING on purpose -- the race where the proc is gone but
        # run()'s finally has not fired yet.
        resp = client.post("/api/tools/terminal/cancel", json={"run_id": run_id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["stopped"] is True
        assert "already gone" in body["detail"].lower()
    finally:
        _reap(proc)


def test_cancel_an_unknown_id_never_500s(client: TestClient) -> None:
    resp = client.post("/api/tools/terminal/cancel", json={"run_id": "nope-nope"})
    assert resp.status_code == 200
    assert resp.json()["stopped"] is False


def test_cancel_all_stops_everything_and_reports_an_honest_count(client: TestClient) -> None:
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(3):
            proc, _ = _spawn_sleeper()
            procs.append(proc)
        listed = client.get("/api/tools/terminal/running").json()
        assert len(listed["running"]) == 3

        resp = client.post("/api/tools/terminal/cancel", json={})  # no run_id -> all
        body = resp.json()
        assert body["stopped"] == 3
        assert "Stopped 3" in body["detail"]

        for proc in procs:
            assert proc.wait(timeout=5) == -15

        # A second cancel-all finds nothing and says zero, not an error.
        again = client.post("/api/tools/terminal/cancel", json={}).json()
        assert again["stopped"] == 0
    finally:
        for proc in procs:
            _reap(proc)


def test_cancel_all_when_nothing_runs_is_a_plain_zero(client: TestClient) -> None:
    # The isolate_running fixture guarantees a genuinely empty table.
    body = client.post("/api/tools/terminal/cancel", json={}).json()
    assert body["stopped"] == 0
    assert "Stopped 0" in body["detail"]


def test_running_reports_only_live_processes(client: TestClient) -> None:
    """A finished command leaves the listing the moment `run()`'s finally pops it.

    A Stop button lit up for a command that is no longer running is exactly the
    dead control this project forbids, so the listing must reflect liveness.
    """
    proc, run_id = _spawn_sleeper()
    try:
        assert client.get("/api/tools/terminal/running").json()["running"] != []
        terminal._RUNNING.clear()
        assert client.get("/api/tools/terminal/running").json()["running"] == []
    finally:
        _reap(proc)
