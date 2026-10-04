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

**Who is allowed to call these, and why it differs per caller.** All five paths
are declared as ``local_paths`` in ``aw-app.json`` (capability
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

Errors are deliberately 502, not 500: every failure mode here is the TV or the
LAN, not this service. A 500 reads as "the app is broken" and sends whoever is
debugging into the wrong half of the system.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .tv import AdbError, TvController

InputName = Literal["hdmi1", "hdmi2", "hdmi3"]


class PowerRequest(BaseModel):
    state: Literal["on", "off"]


def build_routes(config_factory=None) -> FastAPI:
    """``config_factory`` is a zero-arg callable returning the live app config
    (``ctx.config``), not a snapshot — so a settings save is picked up on the
    next request instead of at the next activation."""
    controller = TvController(config_factory or (lambda: {}))
    app = FastAPI(title="xiaomi")

    @app.get("/tv/status")
    async def tv_status() -> dict:
        """Never raises: HA polls this for switch state, and a 502 every 30s
        while the TV is simply unplugged is noise, not information. An
        unreachable or unauthorized TV is reported in the body."""
        return await controller.status()

    @app.post("/tv/power")
    async def tv_power(body: PowerRequest) -> dict:
        try:
            if body.state == "on":
                return await controller.power_on()
            return await controller.power_off()
        except AdbError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/tv/input/{name}")
    async def tv_input(name: InputName) -> dict:
        try:
            return await controller.select_input(name)
        except AdbError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return app
