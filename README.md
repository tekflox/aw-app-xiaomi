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
| `POST` | `/alexa/skill` | Alexa request JSON | Alexa response JSON — see [Alexa voice control](#alexa-voice-control) |
| `GET`  | `/alexa/health` | — | `{ok, signature_verification, skill_id_configured, device_name}` |

`on` wakes the TV if asleep and switches to `default_input`. `off` sends the
power toggle **only** if the TV reports itself `Awake`. `GET /tv/status` never
returns 5xx — an unreachable or unauthorized TV is reported in the body,
because Home Assistant polls it every 30s. The two write routes return **502**
on an adb failure: the fault is the TV or the LAN, not this service.

### Who can call it

- **Loopback inside the workspace container** (an agent, a terminal) — no
  credential. All five `/tv/*` paths are declared as `local_paths` (capability
  `routes:local`). The bypass is loopback-only and matches declared paths as
  **exact literals** (`src/apps/runtime.py`, a frozenset, no wildcards) — which
  is why the three inputs are enumerated in the manifest instead of written as
  a pattern. `tests/test_routes.py` fails if a declared path is one the app
  doesn't serve, because that typo fails *open*.
- **Everyone else, Home Assistant included** — `X-Api-Key`, either the
  workspace master key or (on a core that has `src/api/scoped_api_keys.py`) an
  `awsk_` **scoped key** whose scope covers the route. `IdentityGuard`
  validates and scope-checks it before app code runs. HA is a sibling
  container, not loopback, so it gets no bypass. See
  `aw-app-template/docs/app-workspace-api-auth.md`.
- **Amazon, on `/alexa/skill` only** — no `X-Api-Key` at all, because Alexa
  cannot send one. Authenticated by request signature instead; see below.

#### Why this app carries its own `/tv/*` gate

`auth_required` is a per-**app** switch, and `/alexa/skill` has to answer a
caller that can present no credential of ours. So the app runs in the
framework's `auth_required: false` mode — "the app's own auth is the final
gate", the same mode `mcp-gateway`'s `admin/config` uses — and that mode is
app-wide: on its own it would leave `/tv/power` open to the entire internet,
which is the state this app shipped in and **v0.5.0 closed**.

`routes._require_credential` is therefore a dependency on the `/tv/*` routes
that asks exactly one question: *did the framework authenticate this caller?*
It reads `scope["aw_identity"]` — the verdict `IdentityGuard` already
reached — and never inspects a credential itself. Consequences worth knowing:

- Home Assistant's existing master-key setup keeps working untouched.
- An `awsk_` scoped key starts working here the moment core supports one, with
  **no change to this app** — `IdentityGuard` sets `aw_identity` for a
  scope-checked scoped key exactly as it does for the master key.
- The loopback arm uses the same predicate as core's own `_local_bypass`, on
  purpose. A loopback caller is let through by the framework *before* this
  dependency runs and arrives with no claims, so if the two disagreed about
  what "loopback" means, agents and the smoke test would break.

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

Plus, for the skill: `alexa_skill_id` (**required** for `/alexa/skill` to
accept anything — see below) and `alexa_device_name` (default `monitor`, what
Alexa *says*, not what you say).

Give the TV a DHCP reservation — this app finds it by IP.

## Alexa voice control

Say **"Alexa, tell TV Control to turn on the monitor"** and the TV comes on.

### Why the phrasing, and why there is no Lambda

The bare phrase *"Alexa, turn on the monitor"* is an **Alexa Smart Home
Skill** capability, and Amazon offers Smart Home skills exactly one endpoint
type: an **AWS Lambda ARN**. There is no HTTPS option for that skill type, and
no header, setting or workaround changes that — it is Amazon's platform rule.

Lambda is vetoed here, so this is an **Alexa Custom Skill**, which *does*
support a plain HTTPS endpoint and uses no AWS at all. The cost is the
phrasing: every command goes through the invocation name.

| | Smart Home skill (needs Lambda) | Custom skill (this app) |
|---|---|---|
| Turn on | "Alexa, turn on the monitor" | "Alexa, tell TV Control to turn on the monitor" |
| Turn off | "Alexa, turn off the monitor" | "Alexa, tell TV Control to turn off the monitor" |
| Status | "Alexa, is the monitor on?" | "Alexa, ask TV Control if the monitor is on" |
| Endpoint | AWS Lambda only | any HTTPS URL |

That trade was made deliberately and accepted. It is not a bug to file.

### What authenticates Alexa — and why it isn't an API key

**Nothing the workspace issues.** Amazon calls a Custom skill's HTTPS endpoint
with its own headers and offers no place to configure one of ours, so no
credential of ours — master key, scoped `awsk_` key, identity JWT — can travel
on the inbound hop. A scoped API key is *structurally impossible* here; that is
why `/alexa/skill` is the one route on this app with no `X-Api-Key` check, and
why `/tv/*` had to grow its own gate instead.

What Alexa *does* present is a **request signature**, and `xiaomi_app/alexa.py`
verifies it exactly as Amazon's *Hosting a Custom Skill as a Web Service*
requires. All six checks, in this order:

1. `SignatureCertChainUrl` is `https`, host `s3.amazonaws.com`, path under
   `/echo.api/` (case-sensitive, normalised so `..` can't escape), port 443 if
   stated. **Before** the fetch — this is the only check that constrains where
   we make a network call, and a chain an attacker hosts would satisfy every
   later step.
2. Fetch that PEM chain (cached by URL for an hour; the cached bytes are
   re-verified on every request, so a cache hit skips the download, never a
   check).
3. The chain builds a path to a **real public root** — certifi/OpenSSL's
   bundle — is valid *now*, and its leaf carries the SAN `echo-api.amazon.com`.
   One `x509.verification` server-verifier call does all three rather than
   hand-rolled checks, because hand-rolled is where this goes wrong: checking
   the SAN but not the chain accepts any self-signed cert naming that domain.
4. The `Signature` header verifies against the **raw** request body under that
   leaf's key. `Signature-256` (RSA/SHA-256) wins when Amazon sends it;
   `Signature` (RSA/SHA-1) is the documented baseline fallback.
5. `request.timestamp` is within **150 s** — replay protection. Without it one
   captured request turns the TV on forever.
6. `context.System.application.applicationId` equals `alexa_skill_id`.

**Step 6 fails closed, and that is load-bearing.** Steps 1–5 only prove
"Amazon sent this". Any developer on earth can create a skill and point its
endpoint at this URL; without the applicationId check their skill's requests
are as valid as yours. Until `alexa_skill_id` is set, `/alexa/skill` answers
**403** to everything.

No account linking: a stateless command skill has no per-user state to link,
and step 6 already establishes whose skill it is. (Account linking was a
*Smart Home* skill requirement, not a Custom Skill one.)

### Set it up in the Alexa developer console

We don't hold Frederico's Amazon developer login, so these steps are his. The
endpoint is already live and already verifying signatures — it just answers
403 until step 7.

1. **<https://developer.amazon.com/alexa/console/ask>** → *Create Skill*.
2. **Skill name**: `TV Control`. **Primary locale**: `English (US)` — must
   match the locale the Echo is set to, or no utterance ever matches.
3. **Experience**: `Other` → **Model**: `Custom` → **Hosting**: *Provision
   your own*. Click *Next*, then choose the **Start from scratch** template.
   > Do **not** pick *Smart Home* — that is the one that forces a Lambda.
4. **Build → Invocation**: set *Skill Invocation Name* to `tv control`
   (lowercase, two words — Amazon rejects capitals with a generic error).
5. **Build → Interaction Model → JSON Editor**: paste the whole contents of
   [`alexa/interaction-model.json`](alexa/interaction-model.json) over what's
   there, *Save Model*, then **Build Model** and wait for it to finish.
6. **Build → Endpoint**:
   - Select **HTTPS** — *not* "AWS Lambda ARN".
   - **Default Region**:
     `https://xiaomi.app.fredericowu.workspace.aw.tekflox.com/alexa/skill`
   - **SSL certificate type**: pick **"My development endpoint is a sub-domain
     of a domain that has a wildcard certificate from a certificate
     authority"** — the *second* option. **Not** the third (self-signed); there
     is nothing to upload.
     > Why that one: verified live with `openssl s_client` (2026-10-04), this
     > host serves `subject=CN=*.app.fredericowu.workspace.aw.tekflox.com`,
     > `issuer=Let's Encrypt`, `Verify return code: 0 (ok)`. So it is a
     > publicly-trusted **wildcard**, and `xiaomi.app.…` is a sub-domain under
     > it — which is option 2 word for word. Option 1 ("has a certificate from
     > a trusted CA") also validates, since Amazon just performs a normal TLS
     > check and Let's Encrypt is in every public trust store; option 2 is
     > simply the exact description. Either way there is no cert step for you
     > to do — the platform's own Caddy obtains and renews it.
   - *Save Endpoints*.
7. **Copy the Skill ID** — *Build → Endpoint* shows it at the top as
   `amzn1.ask.skill.xxxxxxxx-…`, or use the *Copy Skill ID* link on the
   console's skill list. Paste it into the workspace: **Apps → Xiaomi TV →
   Settings gear → Alexa Skill ID**, and save. This is step 6 of the
   verification above; nothing works until it's set.
8. **Test → Development** toggle on. The skill is now live on every Echo on
   the same Amazon account — no certification and no publishing needed for
   personal use.

### Test plan

```bash
# 1. the endpoint is reachable and bound (anonymous, from anywhere)
curl -s https://xiaomi.app.fredericowu.workspace.aw.tekflox.com/alexa/health
#   -> {"ok":true,"signature_verification":"enabled",
#       "skill_id_configured":true,"device_name":"monitor"}

# 2. an unsigned request is refused — the gate is actually on
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://xiaomi.app.fredericowu.workspace.aw.tekflox.com/alexa/skill \
  -H 'Content-Type: application/json' -d '{}'
#   -> 400   (and the body names the missing SignatureCertChainUrl)

# 3. the TV API is NO LONGER open to the internet
curl -s -o /dev/null -w '%{http_code}\n' \
  https://xiaomi.app.fredericowu.workspace.aw.tekflox.com/tv/status
#   -> 401   (it was 200 before v0.5.0)

# 4. ...but still answers a credentialed caller (this is HA's path)
curl -s https://xiaomi.app.fredericowu.workspace.aw.tekflox.com/tv/status \
  -H "X-Api-Key: $AW_WORKSPACE_API_KEY"
#   -> {"reachable":true,...}

# 5. the cert Amazon will check is from a public CA
echo | openssl s_client -connect \
  xiaomi.app.fredericowu.workspace.aw.tekflox.com:443 2>/dev/null \
  | grep -E 'Verify return code|issuer'
#   -> subject=CN = *.app.fredericowu.workspace.aw.tekflox.com
#      issuer=C = US, O = Let's Encrypt, CN = YE1
#      Verify return code: 0 (ok)
```

Then, out loud, to any Echo on the account:

| Say | Expect |
|---|---|
| "Alexa, tell TV Control to turn on the monitor" | *"Turning on the monitor now."* — TV wakes, switches to `default_input` |
| "Alexa, tell TV Control to turn off the monitor" | *"Turning off the monitor now."* — screen goes dark |
| "Alexa, ask TV Control if the monitor is on" | *"The monitor is on."* / *"...is off."* — read from the TV itself, not from the last command sent |
| "Alexa, open TV Control" | welcome prompt, session stays open; then just *"turn on the monitor"* |
| "Alexa, ask TV Control for help" | the help prompt, session stays open |

If a command is heard but nothing happens, `GET /tv/status` with a credential
is the first thing to read: `authorized: false` means the adb key problem
below, not the skill.

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
pytest tests/ -q --cov=xiaomi_app          # 162 tests, no TV and no Echo needed
python3 tests/validate_manifest.py aw-app.json
```

Every `adb` invocation is faked. The tests exist for the cases a live smoke
test *passes by luck*: the toggle not being sent to an already-awake TV, the
preamble ordering, two concurrent sequences not interleaving, and
`unauthorized` being reported as its own state.

`tests/test_alexa.py` is the security-critical half — each of Amazon's six
checks gets its own failing case, because both ways the signature check can
break are expensive: too strict and every real command dies with "the skill is
having trouble", too lax and anyone who can POST JSON drives the TV. A
**self-signed two-cert chain** stands in for Amazon's, which is sound only
because `verify_certificate_chain` takes its trust store as an argument and
defaults it to the real certifi/OpenSSL bundle — so a test has to pass its own
root in explicitly, and
`test_the_production_path_does_not_trust_the_test_root` pins that the default
path refuses it.

`tests/test_routes.py` additionally pins the manifest↔routes agreement, and
`tests/test_alexa.py` pins `alexa/interaction-model.json` against the intents
`alexa.handle` dispatches on — that file is uploaded **by hand** in the
developer console, so nothing at runtime would ever notice the two drifting
apart. A rename on either side would leave Alexa matching an utterance to an
intent this code answers "I don't know how to do that yet" to, which reads as
a broken TV rather than a config mismatch.
