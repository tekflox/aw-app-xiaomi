"""
This app's mode-agnostic FastAPI sub-app.

``build_routes(config_factory)`` returns the SAME sub-app in both modes:

* **integrated** — ``plugin.py`` hands it to ``ctx.routes.register(...)``,
  mounted by the runtime at ``/api/apps/xiaomi`` behind ``IdentityGuard``.
  Apps never implement their own auth in this mode.
* **standalone** — ``__main__.py`` mounts it at the same prefix, unguarded,
  bound to loopback.

Paths here are RELATIVE (no ``/api/apps/xiaomi`` prefix) — the runtime adds it:

    POST /api/apps/xiaomi/tv/power          {"state": "on" | "off"}
    POST /api/apps/xiaomi/tv/input/hdmi2
    GET  /api/apps/xiaomi/tv/status
    POST /api/apps/xiaomi/alexa/skill       Alexa Custom Skill fulfillment
    GET  /api/apps/xiaomi/alexa/health      is the skill wired up?

**Who is allowed to call these, and why it differs per caller.** The five
``/tv/*`` paths are declared as ``local_paths`` in ``aw-app.json`` (capability
``routes:local``), so a caller from ``127.0.0.1`` inside the workspace
container — an agent, a terminal, this app's own smoke test — reaches them with
no token. That bypass is loopback-only and exact-path-only
(``src/apps/runtime.py``: the declared paths are a frozenset, matched whole,
with no wildcards — which is why all three inputs are enumerated in the
manifest rather than written as one pattern).

Home Assistant is a **sibling container**, not loopback, so it gets none of
that and authenticates like any other outside caller: ``X-Api-Key:
<workspace API key>``, which ``IdentityGuard``/``require_identity`` accepts
before app code runs (``src/api/identity.py``). HA keeps the key in its own
``secrets.yaml``; nothing about it lives in this app.

**Why this app carries its own ``/tv/*`` gate (:func:`_require_credential`).**
Because ``auth_required`` is per-APP and ``/alexa/skill`` must be anonymous.
Amazon calls a Custom skill's HTTPS endpoint with its own headers and no slot
for one of ours, so Alexa can present no workspace credential of any kind —
the endpoint has to be reachable with no ``X-Api-Key`` at all, which means
this app runs in the framework's ``auth_required: false`` mode ("the app's own
auth is the final gate", the same mode mcp-gateway's ``admin/config`` uses).
That mode is app-wide: it would leave ``/tv/power`` open to the whole internet
too, which is exactly the hole this app shipped with and this gate closes.

The gate deliberately asks only "did the FRAMEWORK authenticate this caller",
never "which credential was it" — ``scope["aw_identity"]`` is set by
``IdentityGuard`` for an identity JWT, for the workspace master key, and (on a
core that has ``src/api/scoped_api_keys.py``) for an ``awsk_`` scoped key that
has already been scope-checked against this route. So a scoped key starts
working here the moment core supports one, with no change in this file. The
loopback arm is the same predicate core's own ``_local_bypass`` uses, on
purpose: a loopback caller is let through by the framework BEFORE this
dependency runs and arrives with no claims, so the two have to agree on what
"loopback" means or agents and HA break.

Errors are deliberately 502, not 500: every failure mode here is the TV or the
LAN, not this service. A 500 reads as "the app is broken" and sends whoever is
debugging into the wrong half of the system.
"""
from __future__ import annotations

from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import alexa
from .tv import AdbError, TvController

InputName = Literal["hdmi1", "hdmi2", "hdmi3"]

#: Same hosts ``IdentityGuard._local_bypass`` treats as loopback — see the
#: module docstring for why these must not drift apart.
_LOCAL_HOSTS = ("127.0.0.1", "::1")


class PowerRequest(BaseModel):
    state: Literal["on", "off"]


def _require_credential(request: Request) -> None:
    """401 unless the framework authenticated this caller, or it is loopback.

    Not app-invented auth: it re-reads the verdict ``IdentityGuard`` already
    reached (``scope["aw_identity"]``) instead of checking any credential
    itself. See the module docstring.
    """
    if request.scope.get("aw_identity") is not None:
        return
    client = request.scope.get("client")
    if client and client[0] in _LOCAL_HOSTS:
        return
    raise HTTPException(
        status_code=401,
        detail="unauthorized: present X-Api-Key (workspace or scoped key) "
               "or a signed-in workspace session",
    )


def build_routes(config_factory=None) -> FastAPI:
    """``config_factory`` is a zero-arg callable returning the live app config
    (``ctx.config``), not a snapshot — so a settings save is picked up on the
    next request instead of at the next activation."""
    config = config_factory or (lambda: {})
    controller = TvController(config)
    app = FastAPI(title="xiaomi")
    guarded = [Depends(_require_credential)]

    def _device() -> str:
        return str((config() or {}).get("alexa_device_name") or "monitor")

    @app.get("/tv/status", dependencies=guarded)
    async def tv_status() -> dict:
        """Never raises: HA polls this for switch state, and a 502 every 30s
        while the TV is simply unplugged is noise, not information. An
        unreachable or unauthorized TV is reported in the body."""
        return await controller.status()

    @app.post("/tv/power", dependencies=guarded)
    async def tv_power(body: PowerRequest) -> dict:
        try:
            if body.state == "on":
                return await controller.power_on()
            return await controller.power_off()
        except AdbError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/tv/input/{name}", dependencies=guarded)
    async def tv_input(name: InputName) -> dict:
        try:
            return await controller.select_input(name)
        except AdbError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # -- Alexa Custom Skill ------------------------------------------------
    #
    # NOT guarded by `_require_credential`, and that is the whole point: see
    # the module docstring. Its gate is Amazon's request signature, in
    # `alexa.verify_request`.

    @app.post("/alexa/skill")
    async def alexa_skill(request: Request):
        raw = await request.body()
        try:
            payload = await alexa.verify_request(
                request.headers, raw,
                str((config() or {}).get("alexa_skill_id") or ""),
            )
        except alexa.AlexaVerificationError as exc:
            # A plain 4xx, never a 5xx and never a spoken response: an
            # unverified request has not earned one. Amazon's docs ask for a
            # 400 on a failed validation.
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await alexa.handle(payload, controller, _device())

    @app.get("/alexa/health")
    async def alexa_health() -> dict:
        """Is the skill wired up? Deliberately says nothing a caller couldn't
        learn by sending a request — it exists so the endpoint can be
        confirmed reachable and bound without a real Alexa device, which is
        otherwise unverifiable from a terminal."""
        return {
            "ok": True,
            "signature_verification": "enabled",
            "skill_id_configured": bool((config() or {}).get("alexa_skill_id")),
            "device_name": _device(),
        }

    return app
