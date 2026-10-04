"""
adb-over-network control of the Xiaomi TV.

Ported verbatim from the four shell scripts that lived in ``~/bin/tv-remote/``
on the Mac (still there as the operator's fallback). Each one did the same
preamble and then one `adb shell` call:

    adb disconnect <host:port>; adb connect <host:port>; sleep 1
    STATE = adb -s <t> shell dumpsys power | grep mWakefulness=
    wake:  if Asleep -> adb -s <t> shell input keyevent 26; sleep 3
    input: adb -s <t> shell am start -a android.intent.action.VIEW \
               -d "content://android.media.tv/passthrough/\
                   com.mediatek.tvinput%2F.hdmi.HDMIInputService%2FHW{4|5|6}"
    off:   if Awake  -> adb -s <t> shell input keyevent 26

Three things about that logic are load-bearing and must not be "cleaned up":

* **keyevent 26 is a TOGGLE.** The `dumpsys power` read before sending it is
  the only reason `power on` / `power off` are idempotent. Skip the read and a
  second "on" turns the TV off.
* **The disconnect-then-connect preamble is not paranoia.** The TV drops the
  adb socket on its own schedule (standby, DHCP, its own reboots), and a stale
  socket fails every command with no useful error. Tearing it down first is
  what makes a cold call work.
* **The connection is a process-wide singleton owned by the adb SERVER**, not
  by us. Two sequences interleaving their disconnect/connect race each other
  into exactly the stale-socket state the preamble exists to avoid — hence
  :data:`_SEQUENCE_LOCK`, one lock for every sequence in this process.

The adb auth key is the other thing that is easy to get wrong and impossible
to debug from the outside. A fresh adb client generates a fresh RSA key, and
the TV answers an unknown key by popping an on-screen "allow USB debugging?"
dialog that nobody is standing in front of — every command then fails with the
device stuck in `unauthorized`. So the already-TV-authorized keypair is kept in
this app's durable data dir and handed to adb via ``ADB_VENDOR_KEYS``. The
server reads that variable **once, at server start**, so a server already
running without it will keep failing; :meth:`TvController.reset_server` kills
it so the next command starts one that has the key. That is also why
``GET /tv/status`` reports ``authorized`` separately from ``reachable``: a key
failure looks exactly like a working TV until you read that field.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time

#: Whole-sequence budget. The tunnel edge cuts a request at 30s and Home
#: Assistant's own switch timeout is set to 30 — so a sequence must fail with
#: a real error inside that, not get severed mid-flight.
SEQUENCE_BUDGET_S = 25.0

#: Per-call ceiling, further clamped by whatever is left of the budget.
CALL_TIMEOUT_S = 10.0

#: `adb connect` needs ~1s before the device shows up in `adb devices`.
CONNECT_SETTLE_S = 1.0

#: The TV takes ~3s to come out of standby far enough to accept an intent.
WAKE_SETTLE_S = 3.0

POWER_KEYEVENT = "26"

_INPUT_URI = (
    "content://android.media.tv/passthrough/"
    "com.mediatek.tvinput%2F.hdmi.HDMIInputService%2F{hw}"
)

DEFAULTS: dict[str, object] = {
    "tv_host": "192.168.1.71",
    "tv_port": 5555,
    "adb_path": "adb",
    "default_input": "hdmi1",
    "inputs": {"hdmi1": "HW4", "hdmi2": "HW5", "hdmi3": "HW6"},
}

#: One lock for every adb sequence in this process — see the module docstring.
_SEQUENCE_LOCK = asyncio.Lock()

_WAKEFULNESS_RE = re.compile(r"mWakefulness=(\w+)")


class AdbError(RuntimeError):
    """An adb invocation failed, timed out, or the budget ran out."""


def data_dir() -> str:
    """This app's durable data dir — where the adb keypair lives.

    Under ``AW_WORKSPACE_HOME`` (``fs:workspace-data``), which survives an app
    update, an uninstall/reinstall and a workspace-container recreation. The
    installed package dir does not: an update overwrites it wholesale.
    """
    home = os.environ.get("AW_WORKSPACE_HOME") or os.path.join(
        os.environ.get("AW_WORKSPACE_CONTAINER_DIR", "/opt/aw-workspace"),
        ".aw-workspace",
    )
    return os.path.join(home, "data", "xiaomi")


def adbkey_path() -> str:
    return os.path.join(data_dir(), "adbkey")


class _Deadline:
    """Shared clock for one sequence, so N adb calls can't each take 10s."""

    def __init__(self, budget: float = SEQUENCE_BUDGET_S) -> None:
        self._end = time.monotonic() + budget

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def timeout_for(self, step: str, want: float = CALL_TIMEOUT_S) -> float:
        left = self.remaining()
        if left <= 0:
            raise AdbError(f"{SEQUENCE_BUDGET_S:.0f}s budget exhausted before {step}")
        return min(want, left)

    def sleep_for(self, want: float) -> float:
        """Never sleep past the deadline — a sleep that overruns turns a
        recoverable slow step into a severed request with no error."""
        return max(0.0, min(want, self.remaining()))


class TvController:
    """Stateless w.r.t. the TV; reads live config on every call.

    ``config_factory`` is a callable so a config save through
    ``POST /api/apps/xiaomi/config`` takes effect on the next request without
    re-registering routes.
    """

    def __init__(self, config_factory) -> None:
        self._config_factory = config_factory
        self._server_reset = False

    # -- config ----------------------------------------------------------

    def config(self) -> dict:
        raw = self._config_factory() or {}
        merged = dict(DEFAULTS)
        for key, value in raw.items():
            if key in DEFAULTS and value not in (None, ""):
                merged[key] = value
        return merged

    def target(self) -> str:
        cfg = self.config()
        return f"{cfg['tv_host']}:{cfg['tv_port']}"

    def hw_for(self, name: str) -> str:
        """Internal id of the socket called ``name``, e.g. hdmi2 -> HW5."""
        inputs = self.config()["inputs"] or {}
        hw = inputs.get(name) if isinstance(inputs, dict) else None
        if not hw:
            known = ", ".join(sorted(inputs)) if isinstance(inputs, dict) else ""
            raise AdbError(f"unknown input {name!r} (configured: {known})")
        return str(hw)

    # -- raw adb ---------------------------------------------------------

    def _env(self) -> dict:
        env = dict(os.environ)
        key = adbkey_path()
        if os.path.exists(key):
            # Only set it when the key is really there: pointing
            # ADB_VENDOR_KEYS at a missing file makes adb fall back to its own
            # generated key silently, which is the failure we are avoiding.
            env["ADB_VENDOR_KEYS"] = key
        return env

    def _run(self, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        cmd = [str(self.config()["adb_path"]), *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
                env=self._env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"`adb {' '.join(args)}` timed out after {timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise AdbError(
                f"adb binary {cmd[0]!r} not found — the app's system CLI installer "
                f"may not have run yet (aw-workspace-cli doctor)"
            ) from exc

    async def _adb(self, args: list[str], deadline: _Deadline, step: str) -> str:
        timeout = deadline.timeout_for(step, CALL_TIMEOUT_S)
        proc = await asyncio.to_thread(self._run, args, timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise AdbError(f"{step} failed: {detail or f'adb exit {proc.returncode}'}")
        return proc.stdout or ""

    async def _shell(self, command: str, deadline: _Deadline, step: str) -> str:
        return await self._adb(["-s", self.target(), "shell", command], deadline, step)

    def reset_server(self) -> None:
        """`adb kill-server`, so the next command starts a server that has
        ``ADB_VENDOR_KEYS``. The variable is read at server start only — a
        server already running without it stays unauthorized forever."""
        self._run(["kill-server"], CALL_TIMEOUT_S)

    async def _ensure_server(self) -> None:
        """Once per process, and only when the key exists: anything already
        running was started without the key."""
        if self._server_reset or not os.path.exists(adbkey_path()):
            self._server_reset = True
            return
        self._server_reset = True
        await asyncio.to_thread(self.reset_server)

    # -- connection + state ---------------------------------------------

    async def _reconnect(self, deadline: _Deadline) -> None:
        """The scripts' preamble, verbatim: disconnect, connect, settle.

        `adb disconnect` of a target that isn't connected exits non-zero, which
        is not an error here — tearing down a socket that is already gone is
        the intended outcome either way.
        """
        await asyncio.to_thread(
            self._run, ["disconnect", self.target()], deadline.timeout_for("disconnect", 5.0)
        )
        await self._adb(["connect", self.target()], deadline, "connect")
        await asyncio.sleep(deadline.sleep_for(CONNECT_SETTLE_S))

    async def _device_state(self, deadline: _Deadline) -> str | None:
        """This target's state in `adb devices`: device / unauthorized /
        offline, or None when it isn't listed at all."""
        out = await self._adb(["devices"], deadline, "devices")
        target = self.target()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == target:
                return parts[1]
        return None

    async def _wakefulness(self, deadline: _Deadline) -> str | None:
        """`Awake` / `Asleep` / `Dozing` as the TV itself reports it."""
        out = await self._shell("dumpsys power", deadline, "dumpsys power")
        match = _WAKEFULNESS_RE.search(out)
        return match.group(1) if match else None

    async def _ensure_awake(self, deadline: _Deadline) -> tuple[str | None, str | None]:
        """Returns (before, after). Sends the toggle ONLY when not Awake."""
        before = await self._wakefulness(deadline)
        if before == "Awake":
            return before, before
        await self._shell(f"input keyevent {POWER_KEYEVENT}", deadline, "keyevent 26 (wake)")
        await asyncio.sleep(deadline.sleep_for(WAKE_SETTLE_S))
        return before, await self._wakefulness(deadline)

    async def _select_input(self, name: str, deadline: _Deadline) -> None:
        uri = _INPUT_URI.format(hw=self.hw_for(name))
        await self._shell(
            f'am start -a android.intent.action.VIEW -d "{uri}"',
            deadline,
            f"switch to {name}",
        )

    # -- public operations (each takes the sequence lock) -----------------

    async def status(self) -> dict:
        """Cheap enough for Home Assistant to poll every 30s.

        Deliberately NOT the full disconnect/connect preamble: `adb connect` on
        an already-connected target is a no-op that returns "already
        connected", so the common case costs one round trip. The teardown only
        happens when the target is missing or wedged — which is the only case
        it was ever for.
        """
        async with _SEQUENCE_LOCK:
            deadline = _Deadline()
            await self._ensure_server()
            try:
                await self._adb(["connect", self.target()], deadline, "connect")
                state = await self._device_state(deadline)
                if state not in ("device", "unauthorized"):
                    await self._reconnect(deadline)
                    state = await self._device_state(deadline)
            except AdbError as exc:
                return {
                    "reachable": False,
                    "authorized": False,
                    "wakefulness": None,
                    "target": self.target(),
                    "error": str(exc),
                }
            if state == "unauthorized":
                return {
                    "reachable": True,
                    "authorized": False,
                    "wakefulness": None,
                    "target": self.target(),
                    "error": (
                        "TV reports this adb key as unauthorized — the authorized "
                        f"keypair must be at {adbkey_path()} and the adb server "
                        "restarted (adb kill-server) after it was placed"
                    ),
                }
            if state is None:
                return {
                    "reachable": False,
                    "authorized": False,
                    "wakefulness": None,
                    "target": self.target(),
                    "error": f"{self.target()} is not listed by `adb devices`",
                }
            try:
                wakefulness = await self._wakefulness(deadline)
            except AdbError as exc:
                return {
                    "reachable": True,
                    "authorized": True,
                    "wakefulness": None,
                    "target": self.target(),
                    "error": str(exc),
                }
            return {
                "reachable": True,
                "authorized": True,
                "wakefulness": wakefulness,
                "target": self.target(),
            }

    async def power_on(self) -> dict:
        """Wake if asleep, then switch to the configured default input."""
        async with _SEQUENCE_LOCK:
            deadline = _Deadline()
            await self._ensure_server()
            await self._reconnect(deadline)
            before, after = await self._ensure_awake(deadline)
            name = str(self.config()["default_input"])
            await self._select_input(name, deadline)
            return {
                "ok": True,
                "state": "on",
                "input": name,
                "wakefulness_before": before,
                "wakefulness_after": after,
            }

    async def power_off(self) -> dict:
        """Toggle only if the TV says it is Awake — see the module docstring."""
        async with _SEQUENCE_LOCK:
            deadline = _Deadline()
            await self._ensure_server()
            await self._reconnect(deadline)
            before = await self._wakefulness(deadline)
            after = before
            if before == "Awake":
                await self._shell(
                    f"input keyevent {POWER_KEYEVENT}", deadline, "keyevent 26 (off)"
                )
                after = await self._wakefulness(deadline)
            return {
                "ok": True,
                "state": "off",
                "wakefulness_before": before,
                "wakefulness_after": after,
            }

    async def select_input(self, name: str) -> dict:
        """Wake if asleep, then switch to ``name`` — switching to an input on a
        sleeping TV silently does nothing, so the wake is not optional."""
        async with _SEQUENCE_LOCK:
            deadline = _Deadline()
            self.hw_for(name)  # reject an unknown input before touching the TV
            await self._ensure_server()
            await self._reconnect(deadline)
            before, after = await self._ensure_awake(deadline)
            await self._select_input(name, deadline)
            return {
                "ok": True,
                "input": name,
                "wakefulness_before": before,
                "wakefulness_after": after,
            }
