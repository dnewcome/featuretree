"""onshape_client.py — minimal Onshape REST client (API-key HMAC auth), stdlib only.

The featuretree Onshape backend: drives a Part Studio over the REST API so the IR can be
emitted as a native Onshape feature tree (and exported / round-tripped). Auth per Onshape's
scheme: sign (method, nonce, date, content-type, path, query) with HMAC-SHA256(secret), header
`Authorization: On <accessKey>:HmacSHA256:<b64sig>`.

Credentials via env (never commit):
    ONSHAPE_ACCESS_KEY, ONSHAPE_SECRET_KEY        (from https://dev-portal.onshape.com)
    ONSHAPE_BASE   (default https://cad.onshape.com)

    python onshape_client.py whoami               # validate auth
    python onshape_client.py create "flexisette"  # new doc -> prints did/wid/eid
"""
import base64
import hashlib
import hmac
import json
import os
import random
import string
import sys
import urllib.request
from email.utils import formatdate

BASE = os.environ.get("ONSHAPE_BASE", "https://cad.onshape.com")


def _creds():
    ak = os.environ.get("ONSHAPE_ACCESS_KEY")
    sk = os.environ.get("ONSHAPE_SECRET_KEY")
    if not ak or not sk:
        raise SystemExit("set ONSHAPE_ACCESS_KEY and ONSHAPE_SECRET_KEY (dev-portal.onshape.com)")
    return ak, sk


def _nonce():
    return "".join(random.choices(string.ascii_letters + string.digits, k=25))


def _auth_header(method, path, query, ctype, date, nonce):
    ak, sk = _creds()
    s = "\n".join([method, nonce, date, ctype, path, query]).lower() + "\n"
    sig = base64.b64encode(hmac.new(sk.encode(), s.encode(), hashlib.sha256).digest()).decode()
    return f"On {ak}:HmacSHA256:{sig}"


def request(method, path, query="", body=None):
    """method e.g. 'GET'/'POST'; path begins with /api/...; query is the raw (encoded) string."""
    ctype = "application/json"
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    nonce = _nonce()
    url = BASE + path + (("?" + query) if query else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Date", date)
    req.add_header("On-Nonce", nonce)
    req.add_header("Authorization", _auth_header(method, path, query, ctype, date, nonce))
    req.add_header("Content-Type", ctype)
    req.add_header("Accept", "*/*")   # NOT application/json — that forces the regen-hostile envelope form
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return json.loads(txt) if txt.strip() else {}
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Onshape {method} {path} -> {e.code}: {e.read().decode()[:600]}")


# --- convenience wrappers ---

def whoami():
    return request("GET", "/api/users/sessioninfo")


def create_document(name, public=False):
    doc = request("POST", "/api/documents", body={"name": name, "isPublic": bool(public)})
    did = doc["id"]
    wid = doc["defaultWorkspace"]["id"]
    eid = default_partstudio(did, wid)
    return {"did": did, "wid": wid, "eid": eid, "name": doc["name"]}


def default_partstudio(did, wid):
    els = request("GET", f"/api/documents/d/{did}/w/{wid}/elements")
    ps = [e for e in els if e.get("elementType") == "PARTSTUDIO"]
    if not ps:
        raise SystemExit("no Part Studio in document")
    return ps[0]["id"]


def get_features(did, wid, eid):
    return request("GET", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features")


def eval_featurescript(did, wid, eid, script):
    return request("POST", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/featurescript",
                   body={"script": script, "queries": []})


def face_transient_ids(did, wid, eid, feature_id):
    """Transient ids of the FACE(s) a feature created (e.g. the default plane 'Top') —
    Onshape sketches reference their plane by transient id, not by a query string."""
    script = ("function(context is Context, queries){ return transientQueriesToStrings("
              "evaluateQuery(context, qCreatedBy(makeId(\"%s\"), EntityType.FACE))); }" % feature_id)
    r = eval_featurescript(did, wid, eid, script)
    res = r["result"]
    vals = res.get("message", res).get("value", [])     # robust to envelope or flat form
    return [v.get("message", v).get("value") for v in vals]


def add_feature(did, wid, eid, feature):
    # flat btType feature (with Accept: */*), wrapped as {"feature": ...} — the onpy form.
    return request("POST", f"/api/partstudios/d/{did}/w/{wid}/e/{eid}/features",
                   body={"feature": feature})


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "whoami"
    if cmd == "whoami":
        info = whoami()
        print("authenticated as:", info.get("name"), "<" + str(info.get("email")) + ">")
    elif cmd == "create":
        name = sys.argv[2] if len(sys.argv) > 2 else "featuretree"
        print(json.dumps(create_document(name), indent=2))
    else:
        print(__doc__)
