"""
XiaomiPlugin.activate against a fake AppContext.

The behaviours worth pinning are the ones that fail silently in production:
the CLI's `verify` command getting dropped on the way to the framework (which
downgrades the health check to "a file by that name exists"), and the config
being captured as a snapshot instead of read live (which makes a changed TV
address take effect only after a restart).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from xiaomi_app import tv
from xiaomi_app.plugin import XiaomiPlugin

REPO = Path(__file__).resolve().parent.parent


class FakeCommands:
    def __init__(self):
        self.installed: list[dict] = []

    def install_system_cli(self, name, installer, uninstall=None, verify=None):
        self.installed.append(
            {"name": name, "installer": installer, "uninstall": uninstall, "verify": verify}
        )


class FakeRoutes:
    def __init__(self):
        self.registered = []

    def register(self, app):
        self.registered.append(app)


class FakeCtx:
    def __init__(self, config=None):
        self.package_dir = str(REPO)
        self.config = config if config is not None else {}
        self.commands = FakeCommands()
        self.routes = FakeRoutes()


@pytest.fixture(autouse=True)
def data_dir_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("AW_WORKSPACE_HOME", str(tmp_path))
    return tmp_path / "data" / "xiaomi"


def test_activate_installs_adb_with_its_verify_command():
    ctx = FakeCtx()
    asyncio.run(XiaomiPlugin().activate(ctx))
    assert ctx.commands.installed == [
        {
            "name": "adb",
            "installer": "scripts/install_adb.sh",
            "uninstall": "scripts/uninstall.sh",
            "verify": "adb --version",
        }
    ]


def test_activate_registers_exactly_one_sub_app():
    ctx = FakeCtx()
    asyncio.run(XiaomiPlugin().activate(ctx))
    assert len(ctx.routes.registered) == 1
    paths = {r.path for r in ctx.routes.registered[0].routes}
    assert {"/tv/status", "/tv/power", "/tv/input/{name}"} <= paths


def test_activate_creates_the_durable_data_dir_for_the_adb_key(data_dir_in_tmp):
    asyncio.run(XiaomiPlugin().activate(FakeCtx()))
    assert data_dir_in_tmp.is_dir()


def test_an_existing_key_is_tightened_to_0600(data_dir_in_tmp):
    """adb refuses a world-readable private key, and a `podman cp` of the
    keypair arrives with the source's mode."""
    data_dir_in_tmp.mkdir(parents=True)
    key = data_dir_in_tmp / "adbkey"
    key.write_text("private")
    key.chmod(0o644)
    asyncio.run(XiaomiPlugin().activate(FakeCtx()))
    assert oct(key.stat().st_mode)[-3:] == "600"


def test_a_missing_key_warns_rather_than_failing_activation(caplog, data_dir_in_tmp):
    """The app must still load: the route surface is how an operator verifies
    the TV is unauthorized in the first place."""
    with caplog.at_level("WARNING"):
        asyncio.run(XiaomiPlugin().activate(FakeCtx()))
    assert "no adb key" in caplog.text


def test_the_config_is_read_live_not_snapshotted():
    """POST /config mutates ctx.config in place — a snapshot taken at activate
    would need a restart to pick up a changed TV address."""
    ctx = FakeCtx(config={"tv_host": "10.0.0.1"})
    asyncio.run(XiaomiPlugin().activate(ctx))
    ctx.config["tv_host"] = "10.0.0.2"
    controller = tv.TvController(lambda: ctx.config)
    assert controller.target() == "10.0.0.2:5555"


def test_deactivate_is_a_noop_that_does_not_raise():
    asyncio.run(XiaomiPlugin().deactivate())


# -- manifest/code agreement -------------------------------------------------


def test_the_manifest_entrypoint_resolves_to_this_class():
    manifest = json.loads((REPO / "aw-app.json").read_text())
    module, _, cls = manifest["runtime"]["entrypoint"].partition(":")
    assert module == "xiaomi_app.plugin"
    assert cls == XiaomiPlugin.__name__


def test_the_manifest_declares_every_capability_activate_uses():
    manifest = json.loads((REPO / "aw-app.json").read_text())
    assert {"commands:install", "routes:register", "fs:workspace-data"} <= set(
        manifest["permissions"]
    )


def test_tier1_asks_for_no_container_permission():
    """A Tier-1 app requesting containers:manage would be a high-risk grant it
    never uses, and high-risk grants are what gate marketplace signing."""
    manifest = json.loads((REPO / "aw-app.json").read_text())
    assert manifest["tier"] == "inprocess"
    assert not any(p.startswith(("host:", "containers:")) for p in manifest["permissions"])


def test_the_installer_and_uninstaller_exist_and_are_executable():
    for script in ("scripts/install_adb.sh", "scripts/uninstall.sh"):
        path = REPO / script
        assert path.exists(), script
        assert os.access(path, os.X_OK), f"{script} is not executable"


def test_the_installer_runs_apt_under_sudo():
    """A bare apt-get dies on the apt lock as uid 1001, on every boot, to a
    log nobody reads."""
    body = (REPO / "scripts/install_adb.sh").read_text()
    for line in body.splitlines():
        if line.strip().startswith("apt-get"):
            pytest.fail(f"un-sudo'd apt-get: {line}")
    assert "sudo apt-get install -y --reinstall adb" in body


def test_the_installer_guard_verifies_rather_than_detects():
    """`command -v adb` cannot tell a working adb from a file named adb."""
    body = (REPO / "scripts/install_adb.sh").read_text()
    code = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    assert not any("command -v" in ln for ln in code)
    assert any("adb --version >/dev/null 2>&1" in ln for ln in code)
