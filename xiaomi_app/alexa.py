"""
Alexa Custom Skill fulfillment — the HTTPS endpoint Amazon POSTs to.

**Why a Custom skill and not a Smart Home skill.** "Alexa, turn on the
monitor" as a bare device-control phrase is an Alexa *Smart Home* Skill
capability, and Amazon offers Smart Home skills exactly one endpoint type: an
AWS Lambda ARN. There is no HTTPS option for that skill type. Lambda is vetoed
for this house, so this is a **Custom** skill, which does support a plain
HTTPS endpoint — at the price of the invocation-name phrasing ("Alexa, tell TV
Control to turn on the monitor" instead of "Alexa, turn on the monitor"). That
trade is deliberate and accepted; see the README.

**What authenticates Alexa — and why it is not an API key.** Nothing the
workspace issues. Amazon calls a Custom skill's HTTPS endpoint with its own
headers and offers no place to configure one of ours, so no credential of
ours — master key, scoped ``awsk_`` key, identity JWT — can travel on this
inbound hop. The credential Alexa presents is a **request signature**, and
verifying it exactly as Amazon's "Hosting a Custom Skill as a Web Service"
requires IS the gate:

1. ``SignatureCertChainUrl`` is https, host ``s3.amazonaws.com``, path under
   ``/echo.api/``, port 443 if stated. Checked BEFORE the fetch — this is the
   step that stops an attacker pointing us at a chain they control.
2. Fetch that PEM chain (cached by URL; the URL is part of what step 1
   validated, so the cache key is safe).
3. The chain builds a path to a **real public root** (the system/certifi trust
   store), is valid *now*, and its leaf carries the SAN
   ``echo-api.amazon.com``. All three come from one
   ``x509.verification`` server-verifier call rather than hand-rolled checks.
4. The ``Signature`` header verifies against the **raw** request body under the
   leaf's public key. ``Signature-256`` (RSA/SHA-256) is preferred when Amazon
   sends it; ``Signature`` (RSA/SHA-1) is the documented baseline and remains
   the fallback.
5. ``request.timestamp`` is within :data:`TIMESTAMP_TOLERANCE_S` — replay
   protection. A signature stays valid forever without it.
6. ``context.System.application.applicationId`` equals the configured skill id.

Step 6 **fails closed** when ``alexa_skill_id`` is unset, and that is not an
oversight to soften later. Steps 1–5 only prove "Amazon sent this". Any
developer on earth can create their own skill and point its endpoint at this
URL; without the applicationId check their skill's requests are as valid as
Frederico's and they can turn the TV on and off. The skill id is the only
thing that makes this endpoint *his*.

Only ``cryptography`` (a pinned aw-workspace core dependency) and the stdlib
are used — the cert fetch is ``urllib`` in a thread rather than httpx, so this
module adds no dependency the workspace does not already carry.
"""
from __future__ import annotations

import asyncio
import base64
import json
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import DNSName, load_pem_x509_certificates
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError

#: Amazon's documented host/path for the signing chain. Both are part of the
#: contract, not a convenience: the path check is case-SENSITIVE and the host
#: check is case-insensitive, per the docs.
CERT_CHAIN_HOST = "s3.amazonaws.com"
CERT_CHAIN_PATH_PREFIX = "/echo.api/"

#: The SAN the signing certificate must carry.
ECHO_API_DOMAIN = "echo-api.amazon.com"

#: Replay window. Amazon's documented limit is 150 seconds.
TIMESTAMP_TOLERANCE_S = 150.0

#: This endpoint is anonymous and internet-facing, so both the body it will
#: read and the chain it will fetch are capped. An Alexa request is a couple
#: of KiB and a chain a few; these are orders of magnitude of headroom.
MAX_BODY_BYTES = 256 * 1024
MAX_CHAIN_BYTES = 128 * 1024

CERT_FETCH_TIMEOUT_S = 5.0

#: How long a fetched chain is reused. Amazon explicitly recommends caching by
#: URL. The cached bytes are re-verified (expiry, chain, SAN) on every single
#: request, so a cache hit never skips a check — it only skips the download.
CERT_CACHE_TTL_S = 3600.0

_chain_cache: dict[str, tuple[float, bytes]] = {}
_trust_store: Store | None = None


class AlexaVerificationError(Exception):
    """A request did not prove it came from Amazon, or came from the wrong
    skill. Always surfaced as a 4xx with this message — never a 5xx: nothing
    here is a fault of this service."""

    def __init__(self, detail: str, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


# -- step 1: the chain URL ---------------------------------------------------


def check_cert_chain_url(url: str | None) -> str:
    """Validate ``SignatureCertChainUrl`` before fetching it.

    This runs first and on the raw header because it is the only step that
    constrains *where we make a network call to*. Fetch first and an attacker
    gets a server-side request to any URL they like, plus a chain they signed
    themselves that would sail through every later check.
    """
    if not url:
        raise AlexaVerificationError("missing SignatureCertChainUrl header")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        raise AlexaVerificationError("SignatureCertChainUrl is not https")
    if (parsed.hostname or "").lower() != CERT_CHAIN_HOST:
        raise AlexaVerificationError(
            f"SignatureCertChainUrl host is not {CERT_CHAIN_HOST}"
        )
    if parsed.port not in (None, 443):
        raise AlexaVerificationError("SignatureCertChainUrl port is not 443")
    # Case-sensitive, and on the NORMALISED path: "/echo.api/../evil/x" parses
    # with a path that passes a naive startswith but resolves elsewhere.
    normalised = urllib.parse.urljoin("/", parsed.path)
    if not normalised.startswith(CERT_CHAIN_PATH_PREFIX):
        raise AlexaVerificationError(
            f"SignatureCertChainUrl path is not under {CERT_CHAIN_PATH_PREFIX}"
        )
    return url


# -- step 2: fetching it -----------------------------------------------------


def _fetch_chain_pem(url: str) -> bytes:
    """Download the PEM chain. Called only with a URL
    :func:`check_cert_chain_url` has already approved."""
    try:
        with urllib.request.urlopen(url, timeout=CERT_FETCH_TIMEOUT_S) as response:
            return response.read(MAX_CHAIN_BYTES + 1)
    except Exception as exc:  # urllib raises a wide family; all mean the same
        raise AlexaVerificationError(
            f"could not fetch the signing certificate chain: {exc}"
        ) from exc


async def fetch_chain(url: str, *, now: float | None = None) -> bytes:
    """Cached-by-URL chain fetch, off the event loop."""
    stamp = time.monotonic() if now is None else now
    cached = _chain_cache.get(url)
    if cached is not None and stamp - cached[0] < CERT_CACHE_TTL_S:
        return cached[1]
    pem = await asyncio.to_thread(_fetch_chain_pem, url)
    if len(pem) > MAX_CHAIN_BYTES:
        raise AlexaVerificationError("signing certificate chain is implausibly large")
    _chain_cache[url] = (stamp, pem)
    return pem


# -- step 3: chain of trust + SAN + validity --------------------------------


def _default_store() -> Store:
    """The real public root store — certifi's bundle, or OpenSSL's default.

    Built once per process and never from anything in the request. This is
    what makes the production path validate against Amazon's *actual* root
    rather than whatever a test or a caller supplies.
    """
    global _trust_store
    if _trust_store is not None:
        return _trust_store
    pem: bytes | None = None
    try:
        import certifi

        with open(certifi.where(), "rb") as handle:
            pem = handle.read()
    except Exception:
        cafile = ssl.get_default_verify_paths().cafile
        if cafile:
            with open(cafile, "rb") as handle:
                pem = handle.read()
    if not pem:
        raise AlexaVerificationError(
            "no CA trust store available to validate Amazon's certificate chain",
            status_code=500,
        )
    _trust_store = Store(load_pem_x509_certificates(pem))
    return _trust_store


def verify_certificate_chain(pem: bytes, *, now: datetime | None = None,
                             store: Store | None = None):
    """Return the signing (leaf) certificate, or raise.

    One ``build_server_verifier`` call covers all three of Amazon's
    certificate requirements at once — a path to a trusted root, validity at
    ``now``, and the ``echo-api.amazon.com`` SAN — which is why none of them is
    hand-rolled here. Hand-rolled is where this check historically goes wrong:
    checking the SAN but not the chain accepts any self-signed cert naming
    that domain.
    """
    try:
        certs = load_pem_x509_certificates(pem)
    except Exception as exc:
        raise AlexaVerificationError(
            f"signing certificate chain is not valid PEM: {exc}"
        ) from exc
    if not certs:
        raise AlexaVerificationError("signing certificate chain is empty")
    builder = PolicyBuilder().store(store if store is not None else _default_store())
    builder = builder.time(now or datetime.now(timezone.utc))
    verifier = builder.build_server_verifier(DNSName(ECHO_API_DOMAIN))
    try:
        verifier.verify(certs[0], certs[1:])
    except VerificationError as exc:
        raise AlexaVerificationError(
            f"signing certificate chain did not validate for {ECHO_API_DOMAIN}: {exc}"
        ) from exc
    return certs[0]


# -- step 4: the signature over the raw body --------------------------------


def verify_signature(certificate, headers, body: bytes) -> None:
    """Verify ``Signature-256`` (RSA/SHA-256) if present, else ``Signature``
    (RSA/SHA-1, Amazon's documented baseline).

    ``body`` must be the bytes exactly as received. Re-serialising the parsed
    JSON changes key order and whitespace and breaks every signature — which
    is why the route hands the raw body in and parses afterwards.
    """
    candidates = (
        ("Signature-256", hashes.SHA256()),
        ("Signature", hashes.SHA1()),
    )
    for header, algorithm in candidates:
        raw = headers.get(header)
        if not raw:
            continue
        try:
            signature = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise AlexaVerificationError(f"{header} is not valid base64") from exc
        try:
            certificate.public_key().verify(
                signature, body, padding.PKCS1v15(), algorithm
            )
        except InvalidSignature as exc:
            raise AlexaVerificationError(
                f"{header} does not match the request body"
            ) from exc
        return
    raise AlexaVerificationError("missing Signature header")


# -- step 5: the timestamp ---------------------------------------------------


def verify_timestamp(payload: dict, *, now: datetime | None = None) -> None:
    """Reject a replayed request. Without this a single captured request turns
    the TV on forever, because the signature over it never stops being
    valid."""
    raw = ((payload.get("request") or {}) if isinstance(payload, dict) else {}).get(
        "timestamp"
    )
    if not isinstance(raw, str) or not raw:
        raise AlexaVerificationError("request.timestamp is missing")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlexaVerificationError(
            f"request.timestamp is not an ISO-8601 instant: {raw!r}"
        ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    # Absolute, so a timestamp from the FUTURE is rejected too — a clock skew
    # that large is indistinguishable from a forged one.
    if abs((reference - stamp).total_seconds()) > TIMESTAMP_TOLERANCE_S:
        raise AlexaVerificationError(
            f"request.timestamp is outside the {TIMESTAMP_TOLERANCE_S:.0f}s replay window"
        )


# -- step 6: the skill id ----------------------------------------------------


def verify_application_id(payload: dict, expected: str | None) -> None:
    """Confirm the request is from *our* skill.

    Fails closed on an unset ``alexa_skill_id``: see the module docstring for
    why this is the check that makes the endpoint Frederico's rather than
    anyone's.
    """
    if not expected:
        raise AlexaVerificationError(
            "this endpoint is not bound to a skill yet — set `alexa_skill_id` in "
            "the Xiaomi TV app settings to the Skill ID from the Alexa developer "
            "console",
            status_code=403,
        )
    presented = (
        ((payload.get("context") or {}).get("System") or {}).get("application") or {}
    ).get("applicationId")
    if presented != expected:
        raise AlexaVerificationError(
            "request is signed by Amazon but belongs to a different skill",
            status_code=403,
        )


# -- the whole gate ----------------------------------------------------------


async def verify_request(headers, body: bytes, skill_id: str | None, *,
                         now: datetime | None = None, store: Store | None = None,
                         fetch=None) -> dict:
    """Run every check in order and return the parsed payload.

    Order matters and is Amazon's: the URL is validated before anything is
    fetched, and the body is parsed only after its signature holds — so
    nothing downstream ever sees JSON this service has not authenticated.
    """
    if len(body) > MAX_BODY_BYTES:
        raise AlexaVerificationError("request body is too large", status_code=413)
    url = check_cert_chain_url(headers.get("SignatureCertChainUrl"))
    pem = await (fetch or fetch_chain)(url)
    certificate = verify_certificate_chain(pem, now=now, store=store)
    verify_signature(certificate, headers, body)
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise AlexaVerificationError("request body is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AlexaVerificationError("request body is not a JSON object")
    verify_timestamp(payload, now=now)
    verify_application_id(payload, skill_id)
    return payload


# -- responses ---------------------------------------------------------------


def speak(text: str, *, end_session: bool = True,
          reprompt: str | None = None) -> dict:
    """An Alexa response envelope. ``version`` is required by the schema."""
    response: dict = {
        "outputSpeech": {"type": "PlainText", "text": text},
        "shouldEndSession": end_session,
    }
    if reprompt:
        response["reprompt"] = {
            "outputSpeech": {"type": "PlainText", "text": reprompt}
        }
    return {"version": "1.0", "response": response}


#: Intent names the interaction model declares. Kept here, next to the
#: dispatch that implements them, because ``alexa/interaction-model.json`` is
#: uploaded by hand in the developer console — a test pins the two together so
#: a rename can't leave the model asking for an intent nothing handles.
TURN_ON_INTENT = "TurnOnIntent"
TURN_OFF_INTENT = "TurnOffIntent"
GET_STATUS_INTENT = "GetStatusIntent"

_STOP_INTENTS = ("AMAZON.CancelIntent", "AMAZON.StopIntent", "AMAZON.NavigateHomeIntent")


async def handle(payload: dict, controller, device: str = "monitor") -> dict:
    """Turn a verified Alexa request into a spoken answer.

    No account linking: a stateless command skill has no per-user state to
    link, and the applicationId check already establishes whose skill this is.

    Every TV failure is spoken, never raised. An unhandled exception reaches
    Alexa as "the skill is having trouble", which tells Frederico nothing; a
    sentence naming what broke tells him whether to look at the TV or the LAN.
    """
    from .tv import AdbError

    request = payload.get("request") or {}
    kind = request.get("type")

    if kind == "SessionEndedRequest":
        # Amazon requires a 200 with no speech here. Speaking would be an error.
        return {"version": "1.0", "response": {}}

    if kind == "LaunchRequest":
        return speak(
            f"TV control is ready. Say turn on the {device}, turn off the "
            f"{device}, or ask if it is on.",
            end_session=False,
            reprompt=f"Say turn on the {device} or turn off the {device}.",
        )

    if kind != "IntentRequest":
        return speak("Sorry, I can't handle that kind of request.")

    name = (request.get("intent") or {}).get("name")

    if name == "AMAZON.HelpIntent":
        return speak(
            f"Say turn on the {device} to switch it on, turn off the {device} to "
            f"switch it off, or ask if the {device} is on.",
            end_session=False,
            reprompt=f"Say turn on the {device} or turn off the {device}.",
        )

    if name in _STOP_INTENTS:
        return speak("Okay.")

    try:
        if name == TURN_ON_INTENT:
            await controller.power_on()
            return speak(f"Turning on the {device} now.")
        if name == TURN_OFF_INTENT:
            await controller.power_off()
            return speak(f"Turning off the {device} now.")
        if name == GET_STATUS_INTENT:
            status = await controller.status()
            if not status.get("reachable"):
                return speak(f"I can't reach the {device} right now.")
            if not status.get("authorized"):
                return speak(
                    f"The {device} is reachable but it isn't accepting commands "
                    f"from me."
                )
            wakefulness = status.get("wakefulness")
            if wakefulness == "Awake":
                return speak(f"The {device} is on.")
            if wakefulness is None:
                return speak(f"I reached the {device} but it didn't report its state.")
            return speak(f"The {device} is off.")
    except AdbError as exc:
        return speak(f"I couldn't reach the {device}. {exc}")

    return speak("Sorry, I don't know how to do that yet.")
