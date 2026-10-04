"""
TvController — the adb sequences, with every `adb` invocation faked.

What these tests are actually for: the three properties in tv.py's docstring
that a mock CAN prove, and that a live smoke test against the TV would NOT
catch because the TV happened to be in the convenient state.

* keyevent 26 is a TOGGLE, so `power on` on an already-Awake TV must send NO
  keyevent. A live test with the TV asleep passes either way.
* the disconnect/connect preamble must precede every sequence.
* a sequence must be serialized — two concurrent calls must not interleave
  their disconnect/connect.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from xiaomi_app import tv

#: Captured before the fixture patches asyncio.sleep — patching it with a
#: lambda that calls asyncio.sleep is infinite recursion.
_real_sleep = asyncio.sleep


class FakeAdb:
    """Records every argv and answers from a scripted `dumpsys power` state."""

    def __init__(self, *, wakefulness="Asleep", devices_state="device",
                 fail_on=None, flip_on_keyevent=True):
        self.calls: list[list[str]] = []
        self.wakefulness = wakefulness
        self.devices_state = devices_state
        self.fail_on = fail_on or ()
        self.flip_on_keyevent = flip_on_keyevent
        self.env_seen: list[dict] = []

    def run(self, args, timeout, env=None):
        self.calls.append(list(args))
        self.env_seen.append(env or {})
        joined = " ".join(args)
        for needle in self.fail_on:
            if needle in joined:
                return subprocess.CompletedProcess(args, 1, "", f"boom: {needle}")
        if "devices" in args:
            listing = "List of devices attached\n"
            if self.devices_state:
                listing += f"192.168.1.71:5555\t{self.devices_state}\n"
            return subprocess.CompletedProcess(args, 0, listing, "")
        if "dumpsys" in joined:
            return subprocess.CompletedProcess(
                args, 0, f"Power Manager State:\n  mWakefulness={self.wakefulness}\n", ""
            )
        if "keyevent" in joined:
            if self.flip_on_keyevent:
                self.wakefulness = "Asleep" if self.wakefulness == "Awake" else "Awake"
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "connected to 192.168.1.71:5555", "")

    def shell_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if "shell" in c]


@pytest.fixture
def controller(monkeypatch):
    def _make(cfg=None, **fake_kwargs):
        fake = FakeAdb(**fake_kwargs)
        ctrl = tv.TvController(lambda: cfg or {})
        monkeypatch.setattr(
            ctrl, "_run", lambda args, timeout: fake.run(args, timeout, ctrl._env())
        )
        monkeypatch.setattr(asyncio, "sleep", lambda _s: _real_sleep(0))
        ctrl._server_reset = True  # don't kill a real adb server from a test
        return ctrl, fake

    return _make


# -- config ------------------------------------------------------------------


def test_defaults_apply_when_config_is_empty(controller):
    ctrl, _ = controller()
    assert ctrl.target() == "192.168.1.71:5555"
    assert ctrl.hw_for("hdmi1") == "HW4"
    assert ctrl.hw_for("hdmi3") == "HW6"


def test_config_overrides_the_tv_address(controller):
    ctrl, _ = controller({"tv_host": "10.0.0.9", "tv_port": 5556})
    assert ctrl.target() == "10.0.0.9:5556"


def test_blank_config_values_fall_back_instead_of_emptying_the_target(controller):
    """A config form saving "" must not produce `:5555` as the adb target."""
    ctrl, _ = controller({"tv_host": "", "tv_port": None})
    assert ctrl.target() == "192.168.1.71:5555"


def test_input_map_is_configurable_not_hardcoded(controller):
    ctrl, _ = controller({"inputs": {"hdmi1": "HW9"}})
    assert ctrl.hw_for("hdmi1") == "HW9"


def test_unknown_input_raises_before_any_adb_call(controller):
    ctrl, fake = controller()
    with pytest.raises(tv.AdbError, match="unknown input"):
        asyncio.run(ctrl.select_input("hdmi7"))
    assert fake.calls == []


# -- the toggle is only sent when the state says so --------------------------


def test_power_on_wakes_a_sleeping_tv_then_switches_input(controller):
    ctrl, fake = controller(wakefulness="Asleep")
    result = asyncio.run(ctrl.power_on())
    shells = fake.shell_commands()
    assert "input keyevent 26" in shells
    assert any("HDMIInputService%2FHW4" in s for s in shells)
    assert result["wakefulness_before"] == "Asleep"
    assert result["wakefulness_after"] == "Awake"


def test_power_on_sends_no_keyevent_when_already_awake(controller):
    """keyevent 26 is a toggle — an unconditional send turns the TV OFF."""
    ctrl, fake = controller(wakefulness="Awake")
    result = asyncio.run(ctrl.power_on())
    assert not any("keyevent" in s for s in fake.shell_commands())
    assert result["wakefulness_before"] == "Awake"
    assert result["wakefulness_after"] == "Awake"


def test_power_off_toggles_only_when_awake(controller):
    ctrl, fake = controller(wakefulness="Awake")
    result = asyncio.run(ctrl.power_off())
    assert fake.shell_commands().count("input keyevent 26") == 1
    assert (result["wakefulness_before"], result["wakefulness_after"]) == ("Awake", "Asleep")


def test_power_off_sends_no_keyevent_when_already_asleep(controller):
    ctrl, fake = controller(wakefulness="Asleep")
    result = asyncio.run(ctrl.power_off())
    assert not any("keyevent" in s for s in fake.shell_commands())
    assert result["wakefulness_after"] == "Asleep"


def test_power_off_never_switches_an_input(controller):
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.power_off())
    assert not any("am start" in s for s in fake.shell_commands())


# -- the preamble and the intent --------------------------------------------


def test_every_sequence_disconnects_before_connecting(controller):
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.power_on())
    verbs = [c[0] for c in fake.calls]
    assert verbs[0] == "disconnect"
    assert verbs[1] == "connect"


def test_input_switch_uses_the_exact_passthrough_uri(controller):
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.select_input("hdmi2"))
    intent = next(s for s in fake.shell_commands() if "am start" in s)
    assert intent == (
        'am start -a android.intent.action.VIEW -d '
        '"content://android.media.tv/passthrough/'
        'com.mediatek.tvinput%2F.hdmi.HDMIInputService%2FHW5"'
    )


def test_input_switch_wakes_a_sleeping_tv_first(controller):
    """An intent sent to a sleeping TV is silently dropped."""
    ctrl, fake = controller(wakefulness="Asleep")
    asyncio.run(ctrl.select_input("hdmi3"))
    shells = fake.shell_commands()
    assert shells.index("input keyevent 26") < next(
        i for i, s in enumerate(shells) if "am start" in s
    )


def test_power_on_honours_the_configured_default_input(controller):
    ctrl, fake = controller({"default_input": "hdmi3"}, wakefulness="Awake")
    result = asyncio.run(ctrl.power_on())
    assert result["input"] == "hdmi3"
    assert any("HW6" in s for s in fake.shell_commands())


def test_every_tv_command_is_target_scoped(controller):
    """Without -s, adb picks "the only device" and would hit an attached
    phone/emulator instead of the TV."""
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.power_on())
    for call in fake.calls:
        if "shell" in call:
            assert call[:2] == ["-s", "192.168.1.71:5555"]


# -- concurrency -------------------------------------------------------------


def test_concurrent_sequences_do_not_interleave(controller):
    """Two sequences racing their disconnect/connect leave adb in exactly the
    stale-socket state the preamble exists to avoid."""
    ctrl, fake = controller(wakefulness="Awake")

    async def both():
        await asyncio.gather(ctrl.select_input("hdmi1"), ctrl.select_input("hdmi2"))

    asyncio.run(both())
    verbs = [c[0] for c in fake.calls]
    first = verbs.index("disconnect")
    second = verbs.index("disconnect", first + 1)
    # Everything belonging to sequence 1 must land before sequence 2 starts.
    assert "connect" in verbs[first:second]
    assert verbs[second - 1] != "disconnect"


# -- status ------------------------------------------------------------------


def test_status_reports_awake_and_authorized(controller):
    ctrl, _ = controller(wakefulness="Awake")
    assert asyncio.run(ctrl.status()) == {
        "reachable": True,
        "authorized": True,
        "wakefulness": "Awake",
        "target": "192.168.1.71:5555",
    }


def test_status_surfaces_unauthorized_as_its_own_state(controller):
    """The failure mode this field exists for: a key the TV never accepted
    looks identical to a healthy TV from the outside."""
    ctrl, _ = controller(devices_state="unauthorized")
    result = asyncio.run(ctrl.status())
    assert result["reachable"] is True
    assert result["authorized"] is False
    assert "unauthorized" in result["error"]


def test_status_reports_unreachable_when_connect_fails(controller):
    ctrl, _ = controller(fail_on=["connect"])
    result = asyncio.run(ctrl.status())
    assert result == {
        "reachable": False,
        "authorized": False,
        "wakefulness": None,
        "target": "192.168.1.71:5555",
        "error": "connect failed: boom: connect",
    }


def test_status_reports_unreachable_when_the_target_is_not_listed(controller):
    ctrl, _ = controller(devices_state="")
    result = asyncio.run(ctrl.status())
    assert result["reachable"] is False
    assert "not listed" in result["error"]


def test_status_retries_with_a_full_reconnect_when_the_device_is_offline(controller):
    ctrl, fake = controller(devices_state="offline")
    asyncio.run(ctrl.status())
    assert "disconnect" in [c[0] for c in fake.calls]


def test_status_does_not_tear_down_a_healthy_connection(controller):
    """It is polled every 30s by HA — the cheap path must stay cheap."""
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.status())
    assert "disconnect" not in [c[0] for c in fake.calls]


def test_status_reports_a_dumpsys_failure_without_raising(controller):
    ctrl, _ = controller(fail_on=["dumpsys"])
    result = asyncio.run(ctrl.status())
    assert result["authorized"] is True
    assert result["wakefulness"] is None
    assert "dumpsys" in result["error"]


def test_status_leaves_wakefulness_none_when_dumpsys_has_no_such_field(controller):
    ctrl, _ = controller(wakefulness="")
    assert asyncio.run(ctrl.status())["wakefulness"] is None


# -- failures, timeouts, vendor keys -----------------------------------------


def test_a_failing_adb_call_raises_adberror_naming_the_step(controller):
    ctrl, _ = controller(fail_on=["keyevent"], wakefulness="Asleep")
    with pytest.raises(tv.AdbError, match="keyevent 26 \\(wake\\)"):
        asyncio.run(ctrl.power_on())


def test_a_failing_disconnect_is_not_an_error(controller):
    """Disconnecting a target that is already gone exits non-zero, and that is
    the outcome we wanted anyway."""
    ctrl, _ = controller(fail_on=["disconnect"], wakefulness="Awake")
    assert asyncio.run(ctrl.power_on())["ok"] is True


def test_missing_adb_binary_is_a_legible_error():
    """Names the installer, because "adb: not found" on its own sends the
    reader looking at the TV instead of at a system CLI that never installed."""
    ctrl = tv.TvController(lambda: {"adb_path": "/nonexistent/adb"})
    ctrl._server_reset = True
    with pytest.raises(tv.AdbError, match="not found"):
        asyncio.run(ctrl.power_on())
    assert "installer" in asyncio.run(ctrl.status())["error"]


def test_a_timeout_becomes_an_adberror(monkeypatch):
    ctrl = tv.TvController(lambda: {})
    ctrl._server_reset = True

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("adb", 10)

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(tv.AdbError, match="timed out"):
        asyncio.run(ctrl.power_on())


def test_an_exhausted_budget_raises_instead_of_running_past_the_edge_cutoff():
    """The tunnel severs a request at 30s; the sequence must fail first."""
    deadline = tv._Deadline(budget=-1.0)
    with pytest.raises(tv.AdbError, match="budget exhausted"):
        deadline.timeout_for("connect")


def test_each_call_is_clamped_to_what_is_left_of_the_budget():
    deadline = tv._Deadline(budget=2.0)
    assert deadline.timeout_for("connect", 10.0) <= 2.0


def test_sleep_never_overruns_the_deadline():
    assert tv._Deadline(budget=0.5).sleep_for(3.0) <= 0.5


def test_vendor_keys_are_passed_to_adb_when_the_key_exists(controller, monkeypatch, tmp_path):
    key = tmp_path / "adbkey"
    key.write_text("private")
    monkeypatch.setattr(tv, "adbkey_path", lambda: str(key))
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.status())
    assert all(env.get("ADB_VENDOR_KEYS") == str(key) for env in fake.env_seen)


def test_no_vendor_keys_variable_when_the_key_is_absent(controller, monkeypatch, tmp_path):
    """Pointing ADB_VENDOR_KEYS at a missing file makes adb fall back to its
    own generated key silently — exactly the failure being avoided."""
    monkeypatch.setattr(tv, "adbkey_path", lambda: str(tmp_path / "absent"))
    ctrl, fake = controller(wakefulness="Awake")
    asyncio.run(ctrl.status())
    assert all("ADB_VENDOR_KEYS" not in env for env in fake.env_seen)


def test_the_server_is_killed_once_so_it_restarts_holding_the_key(
    controller, monkeypatch, tmp_path
):
    """ADB_VENDOR_KEYS is read at server START — a server already running
    without it stays unauthorized forever."""
    key = tmp_path / "adbkey"
    key.write_text("private")
    monkeypatch.setattr(tv, "adbkey_path", lambda: str(key))
    ctrl, fake = controller(wakefulness="Awake")
    ctrl._server_reset = False
    asyncio.run(ctrl.status())
    asyncio.run(ctrl.status())
    assert [c[0] for c in fake.calls].count("kill-server") == 1


def test_the_server_is_not_killed_when_there_is_no_key_to_pick_up(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setattr(tv, "adbkey_path", lambda: str(tmp_path / "absent"))
    ctrl, fake = controller(wakefulness="Awake")
    ctrl._server_reset = False
    asyncio.run(ctrl.status())
    assert "kill-server" not in [c[0] for c in fake.calls]


def test_data_dir_is_under_workspace_home_so_it_survives_a_reinstall(monkeypatch):
    monkeypatch.setenv("AW_WORKSPACE_HOME", "/opt/aw-workspace/.aw-workspace")
    assert tv.data_dir() == "/opt/aw-workspace/.aw-workspace/data/xiaomi"
    assert tv.adbkey_path().endswith("/data/xiaomi/adbkey")


def test_data_dir_falls_back_to_the_container_dir_when_home_is_unset(monkeypatch):
    monkeypatch.delenv("AW_WORKSPACE_HOME", raising=False)
    monkeypatch.delenv("AW_WORKSPACE_CONTAINER_DIR", raising=False)
    assert tv.data_dir() == "/opt/aw-workspace/.aw-workspace/data/xiaomi"
