"""ghl01 (v0.18): the installable app — manifest + icons, a service worker that keeps only Valeria One's own files and the
offline page (never a signed-in page or an API answer), the install prompt, Web Push with this server's own VAPID keys
(RFC 8291/8292, no Firebase), and Android's assetlinks.json from env (404 when unset, never 500)."""
import base64
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy import select

from _va import refuse_network
from test_web import signup
from valeria_one import __version__, db
from valeria_one.services import push as PS

STATIC = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "static"
LANGS = ("en", "es", "pt", "fr", "de", "fil", "id", "hi", "ar", "ja", "zh")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    refuse_network(monkeypatch)


def png_size(p: Path) -> tuple[int, int]:
    b = p.read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", b[16:24])


def test_manifest_and_its_icons(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["display"] == "standalone" and m["start_url"].startswith("/app") and m["scope"] == "/"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i["purpose"] == "maskable" for i in m["icons"])
    for i in m["icons"]:
        w, h = png_size(STATIC / i["src"].removeprefix("/static/"))
        assert f"{w}x{h}" == i["sizes"], i


def test_service_worker_never_caches_the_app_or_the_api(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "no-store" in r.headers["cache-control"] and r.headers["service-worker-allowed"] == "/"
    js = r.text
    assert f"var VERSION = '{__version__}'" in js and "'vo-shell-' + VERSION" in js           # versioned: a new release empties the old
    shell = re.search(r"var SHELL = \[(.*?)\];", js, re.S).group(1)
    entries = re.findall(r"'([^']*)'", shell)
    assert entries and all(e.startswith("/static/") or e == "?v=" for e in entries) and "OFFLINE" in shell
    assert re.search(r"function cacheable\(url\) \{[^}]*pathname\.indexOf\('/static/'\) === 0", js)
    # the only put() is inside the cacheable() branch; pages fall back to the offline page, never to a stored page
    fetch = js.split("self.addEventListener('fetch'")[1].split("self.addEventListener('push'")[0]
    branch = fetch.split("if (cacheable(url))")[1].split("if (req.mode === 'navigate')")
    assert js.count(".put(") == 1 and ".put(" in branch[0] and ".put(" not in branch[1]
    assert "caches.match(OFFLINE)" in branch[1]
    if shutil.which("node"):                                    # and run the rule itself
        src = js.split("function cacheable")[1].split("\n}\n")[0]
        prog = ("var self={location:{origin:'https://one.test'}};function cacheable" + src + "\n}\n"
                "var u=['/static/app.css','/app','/app/inbox','/v1/contacts','/hooks/x','/inv/t','/offline','https://evil.test/static/x.js'];"
                "console.log(JSON.stringify(u.map(function(p){return cacheable(new URL(p,'https://one.test'));})))")
        out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=20).stdout
        assert json.loads(out) == [True, False, False, False, False, False, False, False]
    assert client.get("/offline").status_code == 200


def test_assetlinks_from_env_or_404(client, monkeypatch):
    assert client.get("/.well-known/assetlinks.json").status_code == 404
    fp = ":".join(["AB"] * 32)
    monkeypatch.setenv("VO_ANDROID_PACKAGE", "mx.valai.one")
    monkeypatch.setenv("VO_ANDROID_SHA256", fp.lower())
    j = client.get("/.well-known/assetlinks.json").json()
    assert j[0]["target"] == {"namespace": "android_app", "package_name": "mx.valai.one", "sha256_cert_fingerprints": [fp]}
    monkeypatch.setenv("VO_ANDROID_SHA256", "not-a-fingerprint")
    assert client.get("/.well-known/assetlinks.json").status_code == 404


def test_the_office_offers_the_app_and_customer_pages_do_not(client):
    signup(client, email="pwa@example.com")
    t = client.get("/app").text
    assert '<link rel="manifest" href="/manifest.webmanifest">' in t and "/static/pwa.js" in t and 'id="pw-bar"' in t
    assert "data-pw-install" in t and 'data-pw="ios"' in t
    assert "data-push" in client.get("/app/settings").text
    with db.tx() as conn:
        slug = conn.execute(select(db.businesses.c.slug)).scalar()
    if slug:
        assert "manifest.webmanifest" not in client.get(f"/s/{slug}").text
    assert "manifest.webmanifest" not in client.get("/offline").text
    for name in ("pwa.js", "sw.js", "flow.js", "campaigns.js"):
        assert (STATIC / name).exists()


# ---------------------------------------------------------------- Web Push
def test_vapid_keys_are_made_once_and_kept_secret():
    pub1, key1 = PS.vapid()
    pub2, key2 = PS.vapid()
    assert pub1 == pub2 and len(PS.unb64u(pub1)) == 65
    with db.tx() as conn:
        v = conn.execute(select(db.instance_kv.c.value).where(db.instance_kv.c.key == "vapid")).scalar()
    assert v["private"].startswith("enc:")                                                   # sealed like every secret


def test_encryption_round_trips_like_a_browser():
    ua = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    p256dh = PS.b64u(PS._pub_bytes(ua.public_key()))
    body = PS.encrypt(b'{"title":"Hola"}', p256dh, PS.b64u(auth))
    assert PS.decrypt(body, ua, auth) == b'{"title":"Hola"}'


def test_subscribe_needs_the_csrf_header_and_notify_signs_and_forgets_gone_devices(client):
    signup(client, email="push@example.com")
    ua = ec.generate_private_key(ec.SECP256R1())
    sub = {"endpoint": "https://push.example/sub/1", "keys": {"p256dh": PS.b64u(PS._pub_bytes(ua.public_key())),
                                                              "auth": PS.b64u(b"0123456789abcdef")}}
    assert client.post("/app/push/subscribe", json=sub).status_code == 403
    page = client.get("/app/settings").text
    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    assert client.post("/app/push/subscribe", json=sub, headers={"X-CSRF": csrf}).json() == {"ok": True}
    assert client.get("/app/push/key").json()["key"] == PS.vapid()[0]
    with db.tx() as conn:
        biz_id = conn.execute(select(db.businesses.c.id)).scalar()
    got = []

    def handler(request):
        got.append(request)
        return httpx.Response(201 if len(got) == 1 else 410)
    h = httpx.Client(transport=httpx.MockTransport(handler))
    assert PS.notify(biz_id, "Salón", "Ana te espera", "/app/inbox", http=h) == 1
    req = got[0]
    assert req.headers["content-encoding"] == "aes128gcm" and req.headers["ttl"]
    auth = req.headers["authorization"]
    jwt, k = re.match(r"vapid t=([^,]+), k=(.+)", auth).groups()
    head, claims, sig = jwt.split(".")
    assert json.loads(PS.unb64u(claims))["aud"] == "https://push.example"
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), PS.unb64u(k))
    raw = PS.unb64u(sig)
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))                       # a valid ES256 signature
    assert json.loads(PS.decrypt(req.content, ua, b"0123456789abcdef"))["body"] == "Ana te espera"
    assert PS.notify(biz_id, "Salón", "otra", http=h) == 0                                    # 410: the device is gone…
    with db.tx() as conn:
        assert conn.execute(select(db.push_subscriptions)).first() is None                  # …and forgotten
    _ = base64


def test_new_words_are_in_all_11_languages():
    base = Path(__file__).resolve().parent.parent / "valeria_one" / "web" / "locales"
    files = {lg: json.loads((base / f"{lg}.json").read_text(encoding="utf-8")) for lg in LANGS}
    new = [k for k in files["en"] if k.startswith(("pw_", "sr_", "start_setups", "nx_campaigns", "nx_setups", "nx_invoices",
                                                   "nx_voice", "more_tools"))]
    assert len(new) > 25
    for lg in LANGS:
        for k in new:
            assert files[lg].get(k, "").strip(), (lg, k)
            assert re.findall(r"\{\w+\}", files[lg][k]) == re.findall(r"\{\w+\}", files["en"][k]), (lg, k)
