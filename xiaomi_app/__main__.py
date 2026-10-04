"""
Standalone entrypoint — run this app without the aw-workspace runtime:

    python -m xiaomi_app                 # binds 127.0.0.1:9410
    PORT=9411 python -m xiaomi_app

Mounts the SAME ``build_routes()`` sub-app at the SAME prefix as integrated
mode, so a client's URLs don't change between the two. Config comes from
``XIAOMI_TV_HOST`` / ``XIAOMI_TV_PORT`` / ``XIAOMI_ADB_PATH`` /
``XIAOMI_DEFAULT_INPUT`` here, since there is no workspace config store to
read — anything unset falls back to ``tv.DEFAULTS``.

Auth: no ``IdentityGuard`` — that is runtime machinery, not app code. This
binds loopback only, and nothing here authenticates. Do not expose it.
"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI

from .routes import build_routes

SLUG = "xiaomi"  # must match aw-app.json's "id"
DEFAULT_PORT = 9410  # must match aw-app.json's runtime.standalone.default_port


def env_config() -> dict:
    cfg: dict[str, object] = {}
    for env_name, key in (
        ("XIAOMI_TV_HOST", "tv_host"),
        ("XIAOMI_ADB_PATH", "adb_path"),
        ("XIAOMI_DEFAULT_INPUT", "default_input"),
    ):
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    port = os.environ.get("XIAOMI_TV_PORT")
    if port:
        cfg["tv_port"] = int(port)
    return cfg


def build_standalone_app() -> FastAPI:
    app = FastAPI(title="xiaomi (standalone)")
    app.mount(f"/api/apps/{SLUG}", build_routes(env_config))
    return app


app = build_standalone_app()


def main() -> None:
    port = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    host = os.environ.get("AW_APP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
