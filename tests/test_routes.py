"""
The HTTP surface, through a TestClient, with TvController stubbed.

Also pins the two manifest facts the routes depend on and that nothing else
would catch: that every path declared in ``local_paths`` is a path this
sub-app actually serves (``local_paths`` is matched as exact literals, so a
typo there fails open — the route keeps working for authenticated callers and
silently 401s the agent/loopback caller it was declared for), and that the
app asks for ``routes:local``.

**On the ``client=`` argument.** ``TestClient`` reports a peer of
``"testclient"`` by default, which ``_require_credential`` correctly treats as
"not loopback, no claims → 401". So the fixture takes an explicit peer and the
default is ``127.0.0.1`` — the agent/loopback caller, which is the case every
pre-existing test here was written for. The 401 is then asserted on purpose by
the gate tests rather than hit by accident in all of them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from xiaomi_app import routes as routes_mod
from xiaomi_app.tv import AdbError

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "aw-app.json").read_text())

LOOPBACK = ("127.0.0.1", 50000)
REMOTE = ("203.0.113.9", 44321)


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
    def _make(raises=None, *, peer=LOOPBACK, config=None):
        stub = StubController(raises=raises)
        monkeypatch.setattr(routes_mod, "TvController", lambda _factory: stub)
        api = TestClient(
            routes_mod.build_routes(lambda: dict(config or {})), client=peer
        )
        return api, stub

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
    api = TestClient(routes_mod.build_routes(lambda: {}), client=LOOPBACK)
    response = api.get("/tv/status")
    assert response.status_code == 200
    assert response.json()["reachable"] is False


def test_status_is_200_even_when_adb_is_missing_entirely(monkeypatch, tmp_path):
    """End-to-end on the REAL controller, not a stub: the one endpoint HA
    polls must answer 200 even when nothing about adb works.

    This is the boot-window case — a recreated workspace container has the
    durable keypair but no adb binary yet — and it reaches the first thing
    status() does, the once-per-process `adb kill-server`.
    """
    key = tmp_path / "adbkey"
    key.write_text("private")
    monkeypatch.setattr("xiaomi_app.tv.adbkey_path", lambda: str(key))
    api = TestClient(
        routes_mod.build_routes(lambda: {"adb_path": "/nonexistent/adb"}),
        client=LOOPBACK,
    )

    response = api.get("/tv/status")

    assert response.status_code == 200
    assert response.json()["reachable"] is False


# -- the /tv/* credential gate ----------------------------------------------
#
# This app runs in the framework's `auth_required: false` mode because
# /alexa/skill has to be anonymous (Amazon can carry no credential of ours),
# and that mode is app-WIDE. Without these the whole TV API is open to the
# internet, which is the hole v0.5.0 closed.


@pytest.mark.parametrize("method, path, body", [
    ("get", "/tv/status", None),
    ("post", "/tv/power", {"state": "on"}),
    ("post", "/tv/input/hdmi1", None),
])
def test_an_anonymous_internet_caller_cannot_reach_the_tv(client, method, path, body):
    api, stub = client(peer=REMOTE)
    response = getattr(api, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 401
    assert stub.calls == []


@pytest.mark.parametrize("method, path, body", [
    ("get", "/tv/status", None),
    ("post", "/tv/power", {"state": "on"}),
    ("post", "/tv/input/hdmi1", None),
])
def test_a_loopback_caller_still_reaches_the_tv_with_no_token(client, method, path, body):
    """The agents, the smoke test and anything else inside the workspace
    container. The framework lets these through before this app's dependency
    runs, so they arrive with no claims and must not be mistaken for the
    anonymous internet caller above."""
    api, stub = client(peer=LOOPBACK)
    response = getattr(api, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 200
    assert stub.calls


@pytest.mark.parametrize("claims", [
    {"sub": "frederico"},                                    # identity JWT
    {"sub": "workspace-api-key", "api_key": True},           # master key
    {"sub": "scoped-api-key", "scoped": True, "rules": []},  # awsk_ scoped key
])
def test_a_framework_authenticated_caller_reaches_the_tv_from_anywhere(
    monkeypatch, claims
):
    """The gate asks only "did the framework authenticate this caller", never
    which credential it was — which is what makes Home Assistant keep working
    today AND makes an ``awsk_`` scoped key work the moment core supports one,
    with no change in routes.py.

    ``_Guard`` stands in for ``IdentityGuard``: setting ``scope["aw_identity"]``
    on an authenticated request is the entire contract this app depends on
    (src/apps/runtime.py).
    """
    class _Guard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                scope["aw_identity"] = claims
            await self.app(scope, receive, send)

    stub = StubController()
    monkeypatch.setattr(routes_mod, "TvController", lambda _f: stub)
    api = TestClient(_Guard(routes_mod.build_routes(lambda: {})), client=REMOTE)

    assert api.get("/tv/status").status_code == 200
    assert api.post("/tv/power", json={"state": "off"}).status_code == 200
    assert stub.calls == [("status",), ("power_off",)]


# -- the Alexa endpoint ------------------------------------------------------


def test_the_alexa_endpoint_is_reachable_without_any_credential(client):
    """The whole reason this app can't just set auth_required: true. An
    unverifiable request still has to reach the handler to be REJECTED by the
    signature check — a 401 here would mean Alexa never gets that far."""
    api, stub = client(peer=REMOTE)
    response = api.post("/alexa/skill", json={"request": {"type": "LaunchRequest"}})
    assert response.status_code != 401
    assert stub.calls == []


def test_an_unsigned_alexa_request_is_rejected_by_the_signature_check(client):
    api, stub = client(peer=REMOTE)
    response = api.post("/alexa/skill", json={"request": {"type": "LaunchRequest"}})
    assert response.status_code == 400
    assert "SignatureCertChainUrl" in response.json()["detail"]
    assert stub.calls == []


def test_a_signed_request_for_an_unbound_endpoint_is_a_403(client, monkeypatch):
    """With no ``alexa_skill_id`` configured the endpoint is inert even for a
    perfectly signed request — fail-closed, because an Amazon signature alone
    only proves Amazon sent it, not whose skill it came from."""
    async def _verified(headers, body, skill_id, **kwargs):
        from xiaomi_app import alexa
        alexa.verify_application_id({"context": {}}, skill_id)
        raise AssertionError("should not get past the application id check")

    monkeypatch.setattr(routes_mod.alexa, "verify_request", _verified)
    api, stub = client(peer=REMOTE)
    response = api.post("/alexa/skill", json={})
    assert response.status_code == 403
    assert "alexa_skill_id" in response.json()["detail"]
    assert stub.calls == []


def test_a_verified_turn_on_request_drives_the_tv_and_speaks(client, monkeypatch):
    """The route's own wiring: verification result -> handler -> Alexa
    envelope, with the configured device name in the speech."""
    payload = {
        "version": "1.0",
        "request": {"type": "IntentRequest", "intent": {"name": "TurnOnIntent"}},
    }

    async def _verified(headers, body, skill_id, **kwargs):
        assert skill_id == "amzn1.ask.skill.abc"
        return payload

    monkeypatch.setattr(routes_mod.alexa, "verify_request", _verified)
    api, stub = client(
        peer=REMOTE,
        config={"alexa_skill_id": "amzn1.ask.skill.abc",
                "alexa_device_name": "big screen"},
    )
    response = api.post("/alexa/skill", json=payload)
    assert response.status_code == 200
    assert stub.calls == [("power_on",)]
    speech = response.json()["response"]["outputSpeech"]["text"]
    assert "big screen" in speech


def test_alexa_health_reports_whether_the_skill_is_bound(client):
    api, _ = client(peer=REMOTE)
    unbound = api.get("/alexa/health").json()
    assert unbound["ok"] is True
    assert unbound["skill_id_configured"] is False

    api, _ = client(peer=REMOTE, config={"alexa_skill_id": "amzn1.ask.skill.abc"})
    bound = api.get("/alexa/health").json()
    assert bound["skill_id_configured"] is True
    assert bound["device_name"] == "monitor"


def test_alexa_health_never_echoes_the_skill_id_itself(client):
    """It is anonymous. Reporting "configured: true" is harmless; reporting
    the id would hand an attacker the one value the fail-closed check in
    verify_application_id exists to keep private."""
    api, _ = client(peer=REMOTE, config={"alexa_skill_id": "amzn1.ask.skill.secret"})
    assert "secret" not in api.get("/alexa/health").text


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
