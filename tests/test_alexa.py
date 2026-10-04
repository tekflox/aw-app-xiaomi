"""
The Alexa Custom Skill gate, which is the security-critical half of this app.

``/alexa/skill`` is anonymous and internet-facing on purpose — Amazon has
nowhere to put a credential of ours — so the signature check IS the auth. Both
ways it can break are expensive: too strict and every real voice command dies
with "the skill is having trouble", too lax and anyone who can POST JSON turns
the TV on. So each of Amazon's six checks gets its own failing case here, not
just a happy path.

A **self-signed two-cert chain** stands in for Amazon's. That is sound only
because the production path never sees it: ``verify_certificate_chain`` takes
``store`` and defaults it to :func:`xiaomi_app.alexa._default_store` — certifi
/ OpenSSL's real root bundle, built from nothing in the request. Every test
that wants its chain trusted has to pass that store in explicitly, which is
what keeps "trusts our test root" from ever being reachable in production.
``test_the_production_path_does_not_trust_the_test_root`` pins exactly that.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import Store

from xiaomi_app import alexa
from xiaomi_app.tv import AdbError

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc)
GOOD_URL = "https://s3.amazonaws.com/echo.api/echo-api-cert-12.pem"
SKILL_ID = "amzn1.ask.skill.11111111-2222-3333-4444-555555555555"


# -- a throwaway chain -------------------------------------------------------


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _make_chain(*, san: str = alexa.ECHO_API_DOMAIN,
                not_before: datetime | None = None,
                not_after: datetime | None = None):
    """(root_cert, leaf_cert, leaf_key) — a minimal CA + server cert.

    The extensions are not decoration: cryptography's server verifier enforces
    RFC 5280, so a root without ``keyCertSign`` or a leaf without
    ``serverAuth``/SAN is rejected before the signature is ever looked at, and
    the test would then pass for the wrong reason.
    """
    start = not_before or (NOW - timedelta(days=1))
    end = not_after or (NOW + timedelta(days=30))

    root_key = _key()
    root = (
        x509.CertificateBuilder()
        .subject_name(_name("aw test root"))
        .issuer_name(_name("aw test root"))
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start - timedelta(days=1))
        .not_valid_after(end + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=True, crl_sign=True,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )

    leaf_key = _key()
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(san))
        .issuer_name(root.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        # Required by RFC 5280 and enforced by cryptography's verifier — a
        # leaf without it is rejected before the SAN or the signature is even
        # looked at, which would make these tests pass for the wrong reason.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    return root, leaf, leaf_key


@pytest.fixture(scope="module")
def chain():
    """Module-scoped: RSA-2048 keygen twice per test would dominate the run."""
    return _make_chain()


@pytest.fixture(scope="module")
def chain_pem(chain):
    root, leaf, _ = chain
    pem = leaf.public_bytes(serialization.Encoding.PEM)
    pem += root.public_bytes(serialization.Encoding.PEM)
    return pem


@pytest.fixture(scope="module")
def store(chain):
    root, _, _ = chain
    return Store([root])


def _sign(key, body: bytes, algorithm=None) -> str:
    return base64.b64encode(
        key.sign(body, padding.PKCS1v15(), algorithm or hashes.SHA1())
    ).decode()


def _payload(*, intent: str | None = "TurnOnIntent", kind: str = "IntentRequest",
             timestamp: datetime | None = None,
             application_id: str = SKILL_ID) -> dict:
    request: dict = {
        "type": kind,
        "requestId": "amzn1.echo-api.request.1",
        "timestamp": (timestamp or NOW).isoformat().replace("+00:00", "Z"),
        "locale": "en-US",
    }
    if kind == "IntentRequest" and intent:
        request["intent"] = {"name": intent, "slots": {}}
    return {
        "version": "1.0",
        "session": {"new": True, "sessionId": "amzn1.echo-api.session.1"},
        "context": {"System": {"application": {"applicationId": application_id}}},
        "request": request,
    }


def _signed(payload: dict, leaf_key, *, algorithm=None) -> tuple[bytes, dict]:
    """Raw body + the headers Amazon would send for it."""
    body = json.dumps(payload).encode()
    header = "Signature-256" if algorithm is hashes.SHA256() else "Signature"
    return body, {
        "SignatureCertChainUrl": GOOD_URL,
        header: _sign(leaf_key, body, algorithm),
    }


# -- step 1: the chain URL ---------------------------------------------------


def test_the_canonical_chain_url_is_accepted():
    assert alexa.check_cert_chain_url(GOOD_URL) == GOOD_URL


def test_an_explicit_443_and_an_odd_host_case_are_accepted():
    """Amazon documents the host comparison as case-insensitive and port 443
    as allowed when stated — rejecting either would 400 real traffic."""
    for url in (
        "https://s3.amazonaws.com:443/echo.api/cert.pem",
        "https://S3.amazonaws.COM/echo.api/cert.pem",
    ):
        assert alexa.check_cert_chain_url(url) == url


@pytest.mark.parametrize("url, because", [
    (None, "no header at all"),
    ("", "empty header"),
    ("http://s3.amazonaws.com/echo.api/cert.pem", "not https"),
    ("https://evil.example.com/echo.api/cert.pem", "wrong host"),
    ("https://s3.amazonaws.com.evil.example.com/echo.api/cert.pem", "suffix host"),
    ("https://s3.amazonaws.com:8443/echo.api/cert.pem", "wrong port"),
    ("https://s3.amazonaws.com/invalid.path/cert.pem", "path not under /echo.api/"),
    ("https://s3.amazonaws.com/ECHO.API/cert.pem", "path case must match"),
    ("https://s3.amazonaws.com/echo.api/../evil/cert.pem", "traversal out of the prefix"),
])
def test_a_chain_url_we_must_not_fetch_is_rejected(url, because):
    """Rejected BEFORE any network call: this is the only check that decides
    where we make a request to, and a chain an attacker hosts would satisfy
    every later step."""
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.check_cert_chain_url(url)


# -- step 3: chain of trust, SAN, validity -----------------------------------


def test_a_trusted_chain_with_the_echo_san_validates(chain_pem, store, chain):
    _, leaf, _ = chain
    got = alexa.verify_certificate_chain(chain_pem, now=NOW, store=store)
    assert got.fingerprint(hashes.SHA256()) == leaf.fingerprint(hashes.SHA256())


def test_a_chain_naming_some_other_domain_is_rejected(store):
    """The SAN check is what ties the cert to Alexa rather than to any cert
    the same CA ever issued."""
    root, leaf, _ = _make_chain(san="not-echo-api.example.com")
    pem = leaf.public_bytes(serialization.Encoding.PEM)
    pem += root.public_bytes(serialization.Encoding.PEM)
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(pem, now=NOW, store=Store([root]))


def test_a_chain_that_reaches_no_trusted_root_is_rejected(chain_pem):
    """The failure mode of a hand-rolled check: a self-signed cert naming
    echo-api.amazon.com passes a SAN test and must still be refused."""
    other_root, _, _ = _make_chain()
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(chain_pem, now=NOW, store=Store([other_root]))


def test_an_expired_chain_is_rejected(chain_pem, store):
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(
            chain_pem, now=NOW + timedelta(days=400), store=store
        )


def test_a_chain_not_yet_valid_is_rejected(chain_pem, store):
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(
            chain_pem, now=NOW - timedelta(days=400), store=store
        )


@pytest.mark.parametrize("pem", [b"", b"not a certificate at all"])
def test_a_chain_that_is_not_pem_is_a_4xx_not_a_crash(pem, store):
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(pem, now=NOW, store=store)


# -- step 2: fetching the chain ---------------------------------------------


@pytest.fixture(autouse=True)
def _clear_chain_cache():
    alexa._chain_cache.clear()
    yield
    alexa._chain_cache.clear()


@pytest.mark.asyncio
async def test_a_chain_is_fetched_once_and_then_reused(monkeypatch):
    """Amazon explicitly recommends caching by URL. Every request re-verifies
    the cached bytes (expiry, chain, SAN), so the cache skips the download and
    never a check."""
    fetches = []

    def _fetch(url):
        fetches.append(url)
        return b"-----BEGIN CERTIFICATE-----\n"

    monkeypatch.setattr(alexa, "_fetch_chain_pem", _fetch)
    await alexa.fetch_chain(GOOD_URL, now=0.0)
    await alexa.fetch_chain(GOOD_URL, now=10.0)
    assert fetches == [GOOD_URL]


@pytest.mark.asyncio
async def test_a_stale_cache_entry_is_refetched(monkeypatch):
    fetches = []
    monkeypatch.setattr(
        alexa, "_fetch_chain_pem", lambda url: (fetches.append(url), b"pem")[1]
    )
    await alexa.fetch_chain(GOOD_URL, now=0.0)
    await alexa.fetch_chain(GOOD_URL, now=alexa.CERT_CACHE_TTL_S + 1)
    assert len(fetches) == 2


@pytest.mark.asyncio
async def test_two_different_urls_do_not_share_a_cache_entry(monkeypatch):
    monkeypatch.setattr(alexa, "_fetch_chain_pem", lambda url: url.encode())
    first = await alexa.fetch_chain(GOOD_URL, now=0.0)
    second = await alexa.fetch_chain(GOOD_URL + "x", now=0.0)
    assert first != second


@pytest.mark.asyncio
async def test_an_implausibly_large_chain_is_refused_and_not_cached(monkeypatch):
    monkeypatch.setattr(
        alexa, "_fetch_chain_pem", lambda url: b"x" * (alexa.MAX_CHAIN_BYTES + 1)
    )
    with pytest.raises(alexa.AlexaVerificationError, match="large"):
        await alexa.fetch_chain(GOOD_URL, now=0.0)
    assert GOOD_URL not in alexa._chain_cache


def test_an_unreachable_chain_url_is_a_4xx_not_a_traceback(monkeypatch):
    """S3 being slow or down must surface as "could not fetch", not as a 500
    that reads like this app is broken."""
    def _boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(alexa.urllib.request, "urlopen", _boom)
    with pytest.raises(alexa.AlexaVerificationError, match="could not fetch"):
        alexa._fetch_chain_pem(GOOD_URL)


def test_the_production_path_does_not_trust_the_test_root(chain_pem):
    """The one test that proves the fixtures above cannot weaken production:
    with no ``store`` argument the real certifi/OpenSSL bundle is used, and
    our throwaway root is not in it."""
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_certificate_chain(chain_pem, now=NOW)


# -- step 4: the signature ---------------------------------------------------


def test_a_correct_sha1_signature_verifies(chain):
    _, leaf, leaf_key = chain
    body = b'{"hello":"world"}'
    alexa.verify_signature(leaf, {"Signature": _sign(leaf_key, body)}, body)


def test_a_correct_sha256_signature_verifies(chain):
    """Amazon has been sending Signature-256 alongside the SHA-1 header."""
    _, leaf, leaf_key = chain
    body = b'{"hello":"world"}'
    alexa.verify_signature(
        leaf,
        {"Signature-256": _sign(leaf_key, body, hashes.SHA256())},
        body,
    )


def test_signature_256_is_preferred_over_signature(chain):
    """Both present: the stronger header must be the one that decides. If the
    SHA-1 header could satisfy the check on its own, an attacker who can only
    forge SHA-1 downgrades us by just also sending it."""
    _, leaf, leaf_key = chain
    body = b'{"hello":"world"}'
    headers = {
        "Signature-256": base64.b64encode(b"wrong" * 60).decode(),
        "Signature": _sign(leaf_key, body),
    }
    with pytest.raises(alexa.AlexaVerificationError, match="Signature-256"):
        alexa.verify_signature(leaf, headers, body)


def test_a_signature_over_different_bytes_is_rejected(chain):
    _, leaf, leaf_key = chain
    headers = {"Signature": _sign(leaf_key, b'{"hello":"world"}')}
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_signature(leaf, headers, b'{"hello":"WORLD"}')


def test_a_signature_from_the_wrong_key_is_rejected(chain):
    _, leaf, _ = chain
    _, _, other_key = _make_chain()
    body = b'{"hello":"world"}'
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_signature(leaf, {"Signature": _sign(other_key, body)}, body)


@pytest.mark.parametrize("headers", [
    {},
    {"Signature": ""},
    {"Signature": "!!!! not base64 !!!!"},
])
def test_a_missing_or_malformed_signature_is_a_4xx_not_a_crash(chain, headers):
    _, leaf, _ = chain
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_signature(leaf, headers, b"{}")


# -- step 5: the timestamp ---------------------------------------------------


def test_a_fresh_timestamp_passes():
    alexa.verify_timestamp(_payload(), now=NOW)


@pytest.mark.parametrize("offset", [
    timedelta(seconds=149),
    timedelta(seconds=-149),
])
def test_a_timestamp_inside_the_window_passes(offset):
    alexa.verify_timestamp(_payload(timestamp=NOW + offset), now=NOW)


def test_a_replayed_request_is_rejected():
    """Without this, one captured request turns the TV on forever — its
    signature never stops being valid."""
    with pytest.raises(alexa.AlexaVerificationError, match="replay"):
        alexa.verify_timestamp(_payload(timestamp=NOW - timedelta(minutes=10)), now=NOW)


def test_a_timestamp_from_the_future_is_rejected():
    """Skew that large is indistinguishable from a forged timestamp."""
    with pytest.raises(alexa.AlexaVerificationError, match="replay"):
        alexa.verify_timestamp(_payload(timestamp=NOW + timedelta(minutes=10)), now=NOW)


@pytest.mark.parametrize("payload", [
    {},
    {"request": {}},
    {"request": {"timestamp": ""}},
    {"request": {"timestamp": "yesterday"}},
    {"request": {"timestamp": 12345}},
])
def test_a_missing_or_unparseable_timestamp_is_rejected(payload):
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_timestamp(payload, now=NOW)


# -- step 6: the skill id ----------------------------------------------------


def test_our_own_skill_id_passes():
    alexa.verify_application_id(_payload(), SKILL_ID)


def test_another_skill_pointed_at_this_url_is_refused():
    """Amazon signs every skill's requests with the same chain, so without
    this check any developer's skill can drive the TV."""
    with pytest.raises(alexa.AlexaVerificationError) as caught:
        alexa.verify_application_id(
            _payload(application_id="amzn1.ask.skill.someone-else"), SKILL_ID
        )
    assert caught.value.status_code == 403


@pytest.mark.parametrize("configured", [None, ""])
def test_an_unconfigured_skill_id_fails_closed(configured):
    """Deliberate: the endpoint is inert until it is bound to a skill, rather
    than accepting anything Amazon signed."""
    with pytest.raises(alexa.AlexaVerificationError) as caught:
        alexa.verify_application_id(_payload(), configured)
    assert caught.value.status_code == 403
    assert "alexa_skill_id" in caught.value.detail


def test_a_payload_with_no_application_block_is_refused():
    with pytest.raises(alexa.AlexaVerificationError):
        alexa.verify_application_id({"request": {}}, SKILL_ID)


# -- the whole gate ----------------------------------------------------------


@pytest.fixture
def verify(chain, chain_pem, store):
    _, _, leaf_key = chain

    async def _fetch(_url):
        return chain_pem

    async def _run(body: bytes, headers: dict, *, skill_id: str = SKILL_ID):
        return await alexa.verify_request(
            headers, body, skill_id, now=NOW, store=store, fetch=_fetch
        )

    return _run, leaf_key


@pytest.mark.asyncio
async def test_a_genuine_request_passes_every_check(verify, chain):
    run, leaf_key = verify
    body, headers = _signed(_payload(), leaf_key)
    payload = await run(body, headers)
    assert payload["request"]["intent"]["name"] == "TurnOnIntent"


@pytest.mark.asyncio
async def test_the_signature_is_checked_against_the_RAW_body(verify):
    """Re-serialising the JSON reorders keys and changes whitespace, so a
    signature over the original no longer matches. This is why the route hands
    the raw bytes in and parses afterwards — getting it wrong rejects 100% of
    real traffic with a message that points at the signature, not the code."""
    run, leaf_key = verify
    payload = _payload()
    original = json.dumps(payload).encode()
    reserialised = json.dumps(payload, indent=2, sort_keys=True).encode()
    assert original != reserialised
    headers = {"SignatureCertChainUrl": GOOD_URL, "Signature": _sign(leaf_key, original)}
    with pytest.raises(alexa.AlexaVerificationError):
        await run(reserialised, headers)


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_rejected_after_the_signature(verify):
    run, leaf_key = verify
    body = b"not json"
    headers = {"SignatureCertChainUrl": GOOD_URL, "Signature": _sign(leaf_key, body)}
    with pytest.raises(alexa.AlexaVerificationError, match="JSON"):
        await run(body, headers)


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_anything_else(verify):
    """This endpoint is anonymous: an unbounded body is free memory for
    anyone who finds the URL."""
    run, _ = verify
    with pytest.raises(alexa.AlexaVerificationError) as caught:
        await run(b"x" * (alexa.MAX_BODY_BYTES + 1), {})
    assert caught.value.status_code == 413


@pytest.mark.asyncio
async def test_a_bad_chain_url_short_circuits_before_the_fetch(chain, store):
    run_fetched = []

    async def _fetch(url):
        run_fetched.append(url)
        return b""

    with pytest.raises(alexa.AlexaVerificationError):
        await alexa.verify_request(
            {"SignatureCertChainUrl": "https://evil.example.com/echo.api/c.pem"},
            b"{}", SKILL_ID, now=NOW, store=store, fetch=_fetch,
        )
    assert run_fetched == []


# -- intent handling ---------------------------------------------------------


class StubController:
    def __init__(self, *, status_body=None, raises=None):
        self.calls: list[str] = []
        self.raises = raises
        self.status_body = status_body or {
            "reachable": True, "authorized": True, "wakefulness": "Awake",
            "target": "192.168.1.71:5555",
        }

    async def power_on(self):
        self.calls.append("power_on")
        if self.raises:
            raise self.raises
        return {"ok": True, "state": "on"}

    async def power_off(self):
        self.calls.append("power_off")
        if self.raises:
            raise self.raises
        return {"ok": True, "state": "off"}

    async def status(self):
        self.calls.append("status")
        if self.raises:
            raise self.raises
        return self.status_body


def _speech(response: dict) -> str:
    return response["response"]["outputSpeech"]["text"]


@pytest.mark.asyncio
async def test_turn_on_intent_powers_the_tv_on_and_says_so():
    stub = StubController()
    response = await alexa.handle(_payload(intent="TurnOnIntent"), stub)
    assert stub.calls == ["power_on"]
    assert "Turning on the monitor" in _speech(response)
    assert response["response"]["shouldEndSession"] is True


@pytest.mark.asyncio
async def test_turn_off_intent_powers_the_tv_off_and_says_so():
    stub = StubController()
    response = await alexa.handle(_payload(intent="TurnOffIntent"), stub)
    assert stub.calls == ["power_off"]
    assert "Turning off the monitor" in _speech(response)


@pytest.mark.asyncio
async def test_the_spoken_device_name_is_configurable():
    stub = StubController()
    response = await alexa.handle(_payload(intent="TurnOnIntent"), stub, device="telly")
    assert "Turning on the telly" in _speech(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("wakefulness, expected", [
    ("Awake", "is on"),
    ("Asleep", "is off"),
    ("Dozing", "is off"),
])
async def test_get_status_intent_speaks_the_tvs_own_reading(wakefulness, expected):
    stub = StubController(status_body={
        "reachable": True, "authorized": True, "wakefulness": wakefulness,
    })
    response = await alexa.handle(_payload(intent="GetStatusIntent"), stub)
    assert expected in _speech(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected", [
    ({"reachable": False, "authorized": False, "wakefulness": None}, "can't reach"),
    ({"reachable": True, "authorized": False, "wakefulness": None}, "isn't accepting"),
    ({"reachable": True, "authorized": True, "wakefulness": None}, "didn't report"),
])
async def test_get_status_intent_distinguishes_the_ways_it_can_fail(body, expected):
    """``status`` never raises, it reports in the body — so each of its three
    bad outcomes needs its own sentence or they all sound like "it's off"."""
    response = await alexa.handle(
        _payload(intent="GetStatusIntent"), StubController(status_body=body)
    )
    assert expected in _speech(response)


@pytest.mark.asyncio
async def test_a_tv_failure_is_spoken_not_raised():
    """An exception reaches Alexa as "the skill is having trouble", which says
    nothing about whether to look at the TV or the LAN."""
    stub = StubController(raises=AdbError("TV did not answer"))
    response = await alexa.handle(_payload(intent="TurnOnIntent"), stub)
    assert "TV did not answer" in _speech(response)


@pytest.mark.asyncio
async def test_a_launch_request_keeps_the_session_open():
    """"Alexa, open TV control" has to leave her listening, or the follow-up
    command is spoken into a closed session."""
    response = await alexa.handle(_payload(kind="LaunchRequest"), StubController())
    assert response["response"]["shouldEndSession"] is False
    assert response["response"]["reprompt"]["outputSpeech"]["text"]


@pytest.mark.asyncio
async def test_session_ended_gets_an_empty_200_with_no_speech():
    """Amazon's contract: speaking on SessionEndedRequest is an error."""
    stub = StubController()
    response = await alexa.handle(_payload(kind="SessionEndedRequest"), stub)
    assert response == {"version": "1.0", "response": {}}
    assert stub.calls == []


@pytest.mark.asyncio
async def test_help_keeps_listening_and_stop_does_not():
    helped = await alexa.handle(
        _payload(intent="AMAZON.HelpIntent"), StubController()
    )
    assert helped["response"]["shouldEndSession"] is False
    stopped = await alexa.handle(
        _payload(intent="AMAZON.StopIntent"), StubController()
    )
    assert stopped["response"]["shouldEndSession"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["AMAZON.FallbackIntent", "SomethingElseIntent"])
async def test_an_unknown_intent_never_touches_the_tv(intent):
    stub = StubController()
    response = await alexa.handle(_payload(intent=intent), stub)
    assert stub.calls == []
    assert "don't know how" in _speech(response)


@pytest.mark.asyncio
async def test_an_unknown_request_type_never_touches_the_tv():
    stub = StubController()
    await alexa.handle(_payload(kind="CanFulfillIntentRequest"), stub)
    assert stub.calls == []


# -- interaction model / handler agreement ----------------------------------
#
# `alexa/interaction-model.json` is uploaded BY HAND in the Amazon developer
# console, so nothing at runtime can notice the two drifting apart. A rename
# on either side would leave Alexa confidently matching an utterance to an
# intent this code answers "I don't know how to do that yet" to — which reads
# as a broken TV, not as a config mismatch.

MODEL = json.loads(
    (__import__("pathlib").Path(__file__).resolve().parent.parent
     / "alexa" / "interaction-model.json").read_text()
)
MODEL_INTENTS = {i["name"] for i in MODEL["interactionModel"]["languageModel"]["intents"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", sorted(
    n for n in MODEL_INTENTS if not n.startswith("AMAZON.")
))
async def test_every_custom_intent_in_the_model_is_actually_handled(intent):
    response = await alexa.handle(_payload(intent=intent), StubController())
    assert "don't know how" not in _speech(response), (
        f"{intent} is in the interaction model but alexa.handle falls through on it"
    )


def test_every_intent_this_code_dispatches_on_is_in_the_model():
    assert {
        alexa.TURN_ON_INTENT, alexa.TURN_OFF_INTENT, alexa.GET_STATUS_INTENT
    } <= MODEL_INTENTS


def test_the_model_declares_the_builtins_the_handler_answers():
    """AMAZON.FallbackIntent in particular: without it in the model, an
    unrecognised phrase fails the whole skill instead of reaching the
    handler's "I don't know how to do that yet"."""
    assert {
        "AMAZON.HelpIntent", "AMAZON.StopIntent", "AMAZON.CancelIntent",
        "AMAZON.FallbackIntent",
    } <= MODEL_INTENTS


def test_the_invocation_name_is_lowercase_multiword_as_amazon_requires():
    """Amazon rejects a model whose invocation name has capitals — and the
    console error for it is generic enough to cost an hour."""
    name = MODEL["interactionModel"]["languageModel"]["invocationName"]
    assert name == name.lower() and name.strip() == name and len(name.split()) >= 2
