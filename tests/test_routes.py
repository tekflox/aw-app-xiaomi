"""
The HTTP surface, through a TestClient, with TvController stubbed.

Also pins the two manifest facts the routes depend on and that nothing else
would catch: that every path declared in ``local_paths`` is a path this
sub-app actually serves (``local_paths`` is matched as exact literals, so a
typo there fails open — the route keeps working for authenticated callers and
silently 401s the agent/loopback caller it was declared for), and that the
app asks for ``routes:local``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xiaomi_app import routes as routes_mod
from xiaomi_app.tv import AdbError

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "aw-app.json").read_text())


class StubController:
    def __init__(self, *, raises=None):
        self.raises = raises
        self.calls: list[tuple] = []

    async def status(self):
        self.calls.append(("status",))
        return {"reachable": True, "authorized": True, "wakefulness": "Awake",
                "target": "192.168.1.71:5555"}

    async def power_on(self):
        self.calls.append(("power_on",))
        if self.raises:
            raise self.raises
        return {"ok": True, "state": "on", "input": "hdmi1",
                "wakefulness_before": "Asleep", "wakefulness_after": "Awake"}

    async def power_off(self):
        self.calls.append(("power_off",))
        if self.raises:
            raise self.raises
        return {"ok": True, "state": "off",
                "wakefulness_before": "Awake", "wakefulness_after": "Asleep"}

    async def select_input(self, name):
        self.calls.append(("select_input", name))
        if self.raises:
            raise self.raises
        return {"ok": True, "input": name,
                "wakefulness_before": "Awake", "wakefulness_after": "Awake"}


@pytest.fixture
def client(monkeypatch):
    def _make(raises=None):
        stub = StubController(raises=raises)
        monkeypatch.setattr(routes_mod, "TvController", lambda _factory: stub)
        return TestClient(routes_mod.build_routes(lambda: {})), stub

    return _make


def test_status_returns_the_controller_reading(client):
    api, stub = client()
    body = api.get("/tv/status").json()
    assert body["wakefulness"] == "Awake"
    assert stub.calls == [("status",)]


def test_power_on_and_off_route_to_the_right_operation(client):
    api, stub = client()
    assert api.post("/tv/power", json={"state": "on"}).json()["state"] == "on"
    assert api.post("/tv/power", json={"state": "off"}).json()["state"] == "off"
    assert stub.calls == [("power_on",), ("power_off",)]


def test_power_response_carries_the_before_and_after_state(client):
    """A 200 is not proof the TV moved — the caller needs the TV's own
    reading, which is also what the e2e check reads."""
    api, _ = client()
    body = api.post("/tv/power", json={"state": "on"}).json()
    assert body["wakefulness_before"] == "Asleep"
    assert body["wakefulness_after"] == "Awake"


def test_an_unknown_power_state_is_rejected_before_touching_the_tv(client):
    api, stub = client()
    assert api.post("/tv/power", json={"state": "maybe"}).status_code == 422
    assert stub.calls == []


def test_a_missing_body_is_a_422_not_a_crash(client):
    api, _ = client()
    assert api.post("/tv/power", json={}).status_code == 422


@pytest.mark.parametrize("name", ["hdmi1", "hdmi2", "hdmi3"])
def test_each_declared_input_is_served(client, name):
    api, stub = client()
    assert api.post(f"/tv/input/{name}").json()["input"] == name
    assert stub.calls == [("select_input", name)]


def test_an_undeclared_input_is_a_422(client):
    api, stub = client()
    assert api.post("/tv/input/scart").status_code == 422
    assert stub.calls == []


def test_an_adb_failure_is_a_502_not_a_500(client):
    """502: the fault is the TV or the LAN, not this service. A 500 sends the
    next person debugging into the wrong half of the system."""
    api, _ = client(raises=AdbError("TV did not answer"))
    response = api.post("/tv/power", json={"state": "on"})
    assert response.status_code == 502
    assert "TV did not answer" in response.json()["detail"]


def test_an_adb_failure_on_an_input_switch_is_also_a_502(client):
    api, _ = client(raises=AdbError("nope"))
    assert api.post("/tv/input/hdmi1").status_code == 502


def test_status_never_propagates_an_error_as_a_5xx(monkeypatch):
    """HA polls this for switch state; a 502 every 30s while the TV is simply
    unplugged is noise, so an unreachable TV is reported in the body."""
    class Unreachable(StubController):
        async def status(self):
            return {"reachable": False, "authorized": False, "wakefulness": None,
                    "target": "x", "error": "no route"}

    monkeypatch.setattr(routes_mod, "TvController", lambda _f: Unreachable())
    api = TestClient(routes_mod.build_routes(lambda: {}))
    response = api.get("/tv/status")
    assert response.status_code == 200
    assert response.json()["reachable"] is False


# -- manifest/route agreement ------------------------------------------------


def test_every_local_path_is_a_path_this_app_actually_serves():
    """``local_paths`` are exact literals in a frozenset (no wildcards), so a
    typo fails OPEN: the route still answers authenticated callers and
    silently 401s the loopback caller the declaration was for."""
    declared = set(MANIFEST["contributes"]["routes"][0]["local_paths"])
    app = routes_mod.build_routes(lambda: {})
    served = {r.path for r in app.routes}
    # /tv/input/{name} is a parameterised route; its concrete instances are
    # what the manifest has to enumerate.
    concrete = {p for p in declared if "/tv/input/" not in p}
    inputs = declared - concrete
    assert concrete <= served
    assert "/tv/input/{name}" in served
    assert inputs == {"/tv/input/hdmi1", "/tv/input/hdmi2", "/tv/input/hdmi3"}


def test_the_input_local_paths_match_the_configured_input_names():
    """Adding an input to config_schema without adding its local_path leaves
    that one input unreachable from a loopback caller."""
    names = set(MANIFEST["config_schema"]["properties"]["inputs"]["default"])
    declared = {
        p.rsplit("/", 1)[1]
        for p in MANIFEST["contributes"]["routes"][0]["local_paths"]
        if "/tv/input/" in p
    }
    assert declared == names


def test_the_manifest_requests_the_capability_local_paths_needs():
    """Without routes:local the declaration is silently inert."""
    assert "routes:local" in MANIFEST["permissions"]


def test_the_route_prefix_matches_the_app_id():
    assert MANIFEST["contributes"]["routes"][0]["prefix"] == f"/api/apps/{MANIFEST['id']}"
