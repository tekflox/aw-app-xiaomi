# aw-app-xiaomi

Tier-1 aw-workspace app (`id: xiaomi`) that turns the Xiaomi TV on/off and
switches its HDMI input over the LAN, and reports the TV's **own** power state
back so a Home Assistant switch reflects reality rather than the last command
sent.

Replaces four shell scripts that lived in `~/bin/tv-remote/` on the Mac and
were driven from Home Assistant over SSH. The SSH path is dead (macOS
Full-Disk-Access); the scripts are still on the Mac as the operator's manual
fallback and are not maintained here.

## API

Mounted by the runtime at `/api/apps/xiaomi`.

| Method | Path | Body | Returns |
|---|---|---|---|
| `POST` | `/tv/power` | `{"state": "on"\|"off"}` | `{ok, state, input?, wakefulness_before, wakefulness_after}` |
| `POST` | `/tv/input/{hdmi1\|hdmi2\|hdmi3}` | — | `{ok, input, wakefulness_before, wakefulness_after}` |
| `GET`  | `/tv/status` | — | `{reachable, authorized, wakefulness, target, error?}` |

`on` wakes the TV if asleep and switches to `default_input`. `off` sends the
power toggle **only** if the TV reports itself `Awake`. `GET /tv/status` never
returns 5xx — an unreachable or unauthorized TV is reported in the body,
because Home Assistant polls it every 30s. The two write routes return **502**
on an adb failure: the fault is the TV or the LAN, not this service.

### Who can call it

- **Loopback inside the workspace container** (an agent, a terminal) — no
  credential. All five paths are declared as `local_paths` (capability
  `routes:local`). The bypass is loopback-only and matches declared paths as
  **exact literals** (`src/apps/runtime.py`, a frozenset, no wildcards) — which
  is why the three inputs are enumerated in the manifest instead of written as
  a pattern. `tests/test_routes.py` fails if a declared path is one the app
  doesn't serve, because that typo fails *open*.
- **Everyone else, Home Assistant included** — `X-Api-Key: <workspace API
  key>`, which `IdentityGuard`/`require_identity` accepts before app code runs.
  HA is a sibling container, not loopback, so it gets no bypass. See
  `aw-app-template/docs/app-workspace-api-auth.md`.

```bash
# from inside the workspace container
curl -s localhost:9030/api/apps/xiaomi/tv/status
curl -s -X POST localhost:9030/api/apps/xiaomi/tv/power \
     -H 'Content-Type: application/json' -d '{"state":"on"}'
```

## Config

`tv_host` (default `192.168.1.71`), `tv_port` (`5555`), `adb_path` (`adb`),
`default_input` (`hdmi1`), and `inputs`, the input→socket-id map
(`hdmi1→HW4`, `hdmi2→HW5`, `hdmi3→HW6`). Nothing TV-specific is hardcoded in
code. Config is read **per request**, so a save through the Settings gear or
`POST /api/apps/xiaomi/config` takes effect with no restart.

Give the TV a DHCP reservation — this app finds it by IP.

## The adb authorization key (the part that silently breaks)

A fresh adb client generates a fresh RSA key, and the TV answers an unknown key
with an on-screen *"allow debugging?"* prompt nobody is standing in front of.
Every command then fails with the device stuck `unauthorized` — which from the
outside looks exactly like a working TV.

So the already-TV-authorized keypair lives in the app's **durable** data dir:

```
<AW_WORKSPACE_HOME>/data/xiaomi/adbkey      # chmod 600 (adb refuses a loose key)
<AW_WORKSPACE_HOME>/data/xiaomi/adbkey.pub
```

That survives an app update, an uninstall/reinstall and a workspace-container
recreation; the installed package dir does not (an update overwrites it
wholesale). `activate()` creates the dir, tightens the key's mode, and warns
loudly if there is no key.

`ADB_VENDOR_KEYS` is read by the adb **server** at server start, not per
command — so a server already running without it stays unauthorized forever.
The app runs `adb kill-server` once per process when the key is present, so the
next command starts a server that holds it. `GET /tv/status` reports
`authorized` separately from `reachable` precisely so this failure is visible.

## Three things not to "clean up"

1. **`keyevent 26` is a TOGGLE.** The `dumpsys power` read before sending it is
   the only reason `on`/`off` are idempotent. Drop the read and a second "on"
   turns the TV off.
2. **The `disconnect`-then-`connect` preamble isn't paranoia.** The TV drops the
   adb socket on its own schedule, and a stale socket fails every command with
   no useful error.
3. **Every sequence takes one process-wide lock.** The adb connection is a
   singleton owned by the adb server; two sequences interleaving their
   disconnect/connect race into the stale-socket state the preamble exists to
   avoid.

A whole sequence is capped at 25s (`tv.SEQUENCE_BUDGET_S`), each adb call
clamped to what's left of it — the tunnel edge severs a request at 30s, and a
severed request carries no error anyone can read.

## Install

`adb` comes from `contributes.system_clis` (`apt-get install adb`, under
`sudo` — the container user is uid 1001 and a bare `apt-get` dies on the apt
lock). Activation re-runs on every boot, which is what makes the app survive a
workspace-container recreation from a fresh image.

```bash
aw-workspace-cli sideload /path/to/aw-app-xiaomi        # dev loop
aw-workspace-cli marketplace install xiaomi             # durable
```

Sideload is for the dev loop only — the reconciler converges to the cloud
registry and will quietly revert it.

## Tests

```bash
pytest tests/ -q --cov=xiaomi_app          # 70 tests, no TV needed
python3 tests/validate_manifest.py aw-app.json
```

Every `adb` invocation is faked. The tests exist for the cases a live smoke
test *passes by luck*: the toggle not being sent to an already-awake TV, the
preamble ordering, two concurrent sequences not interleaving, and
`unauthorized` being reported as its own state.
