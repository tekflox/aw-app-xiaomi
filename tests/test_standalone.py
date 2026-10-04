"""
Standalone mode — the same sub-app at the same prefix, outside the runtime.

The point of the prefix assertion: if standalone mounted at a different path
than the runtime does, every client URL would be mode-specific.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from xiaomi_app.__main__ import DEFAULT_PORT, SLUG, build_standalone_app, env_config

MANIFEST = json.loads((Path(__file__).resolve().parent.parent / "aw-app.json").read_text())


def test_routes_are_served_under_the_same_prefix_as_integrated_mode():
    client = TestClient(build_standalone_app())
    # 405 (not 404) proves the path is mounted and only the verb is wrong.
    assert client.get(f"/api/apps/{SLUG}/tv/power").status_code == 405


def test_slug_and_port_match_the_manifest():
    assert SLUG == MANIFEST["id"]
    assert DEFAULT_PORT == MANIFEST["runtime"]["standalone"]["default_port"]
    assert MANIFEST["runtime"]["standalone"]["module"] == "xiaomi_app"


def test_env_config_maps_the_documented_variables(monkeypatch):
    monkeypatch.setenv("XIAOMI_TV_HOST", "10.1.1.1")
    monkeypatch.setenv("XIAOMI_TV_PORT", "5557")
    monkeypatch.setenv("XIAOMI_DEFAULT_INPUT", "hdmi2")
    assert env_config() == {
        "tv_host": "10.1.1.1",
        "tv_port": 5557,
        "default_input": "hdmi2",
    }


def test_env_config_is_empty_when_nothing_is_set(monkeypatch):
    """An empty dict means tv.DEFAULTS apply — not that the target becomes
    ``:5555``."""
    for name in ("XIAOMI_TV_HOST", "XIAOMI_TV_PORT", "XIAOMI_ADB_PATH",
                 "XIAOMI_DEFAULT_INPUT"):
        monkeypatch.delenv(name, raising=False)
    assert env_config() == {}
