"""
Entrypoint named by aw-app.json's ``runtime.entrypoint``
("xiaomi_app.plugin:XiaomiPlugin").

``activate(ctx)`` does two things, both through gated facades:

1. installs the ``adb`` system CLI via ``ctx.commands``
   (``commands:install``), journaled so an uninstall replays the revert. The
   installer is idempotent because activation re-runs on **every** boot — and
   that re-run is precisely what makes the app survive a workspace-container
   recreation, which starts from a fresh image with no ``adb`` in it.
2. registers the sub-app from ``routes.py`` via ``ctx.routes``
   (``routes:register``), mounted at ``/api/apps/xiaomi``.

The config is passed as a **callable**, not a snapshot: ``POST
/api/apps/xiaomi/config`` mutates ``ctx.config`` in place, so reading it per
request means a changed TV address takes effect immediately with no restart
and no ``on_config_saved`` hook.
"""

from __future__ import annotations

import json
import logging
import os

from . import routes as routes_mod
from .tv import adbkey_path, data_dir

log = logging.getLogger("aw_apps.xiaomi")


class XiaomiPlugin:
    async def activate(self, ctx) -> None:
        with open(os.path.join(ctx.package_dir, "aw-app.json"), encoding="utf-8") as f:
            manifest = json.load(f)

        # The adb keypair lives here (see tv.py's docstring). Create the dir on
        # activate so an operator placing the key has somewhere to put it, and
        # tighten the private key if it arrived with loose permissions — adb
        # refuses to use a world-readable key.
        os.makedirs(data_dir(), exist_ok=True)
        key = adbkey_path()
        if os.path.exists(key):
            os.chmod(key, 0o600)
        else:
            log.warning(
                "xiaomi: no adb key at %s — the TV will answer with an on-screen "
                "'allow debugging?' prompt and every command will fail as "
                "unauthorized until the TV-authorized keypair is placed there",
                key,
            )

        for cli in manifest.get("contributes", {}).get("system_clis", []):
            # `verify` is what lets doctor/missing_system_clis tell a working
            # CLI from a name that happens to be on PATH — always thread it
            # through rather than letting the framework fall back to a
            # presence check.
            ctx.commands.install_system_cli(
                cli["name"], cli["installer"], uninstall="scripts/uninstall.sh",
                verify=cli.get("verify"),
            )

        ctx.routes.register(routes_mod.build_routes(lambda: getattr(ctx, "config", {}) or {}))

        log.info("xiaomi activated: adb CLI installed, routes mounted at /api/apps/xiaomi")

    async def deactivate(self) -> None:
        # The CLI install is reverted by the framework's journal replay
        # (scripts/uninstall.sh). The adb keypair in the data dir is
        # deliberately NOT removed: it is the TV's own authorization, it
        # survives uninstall/reinstall by design, and re-pairing it needs
        # somebody standing in front of the TV.
        log.info("xiaomi deactivated")
