#!/usr/bin/env python3
"""Deploy all RoofRadar workflows to an n8n instance through the n8n public API.

    cp .env.example .env      # fill it in
    python3 scripts/deploy.py                   # create/update credentials + workflows, activate triggers
    python3 scripts/deploy.py --no-activate     # import only
    python3 scripts/deploy.py --dry-run         # show what would happen

What it does
  1. Credentials: uses CRED_<KEY>_ID from .env if set; otherwise creates the credential from the secrets in .env
     (Google Calendar is OAuth — create it in the n8n UI and put its id in CRED_GCAL_ID).
     Created ids are remembered in .deploy-state.json so re-runs don't duplicate anything.
  2. Workflows: creates each one (or updates it in place if it was deployed before), with every
     __WF__ / __CRED_ placeholder replaced by real ids and 19 set as the Error Workflow.
  3. Activates every workflow that has a webhook or schedule trigger (sub-workflows don't need activating).
Only the Python standard library is used.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WF_DIR = os.path.join(ROOT, "n8n", "workflows")
STATE = os.path.join(ROOT, ".deploy-state.json")

sys.path.insert(0, os.path.join(ROOT, "n8n", "gen"))
from lib import CREDS  # noqa: E402


def load_env(path=os.path.join(ROOT, ".env")):
    env = dict(os.environ)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = re.split(r"\s+#", v, maxsplit=1)[0]          # drop inline comments
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


class N8N:
    def __init__(self, url, key, dry=False):
        self.base = url.rstrip("/") + "/api/v1"
        self.key = key
        self.dry = dry

    def req(self, method, path, body=None):
        if self.dry and method != "GET":
            print(f"  [dry-run] {method} {path}")
            return {"id": f"dry-{abs(hash(path)) % 10**6}"}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-N8N-API-KEY": self.key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise SystemExit(f"n8n API {method} {path} -> {e.code}: {e.read().decode()[:600]}")


def cred_payloads(env):
    """Credential bodies built from .env secrets (None when the secrets are not provided)."""
    def have(*ks):
        return all(env.get(k) for k in ks)
    p = {}
    if have("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD"):
        p["POSTGRES"] = {"host": env["PGHOST"], "database": env["PGDATABASE"], "user": env["PGUSER"],
                         "password": env["PGPASSWORD"], "port": int(env.get("PGPORT", 5432)),
                         "ssl": env.get("PGSSLMODE", "require"), "allowUnauthorizedCerts": False, "sshTunnel": False}
    if have("OPENAI_API_KEY"):
        p["OPENAI"] = {"apiKey": env["OPENAI_API_KEY"], "header": False}
    if have("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        # (the n8n credential schema also demands the api-key fields to be present, even when empty)
        p["TWILIO"] = {"authType": "authToken", "accountSid": env["TWILIO_ACCOUNT_SID"], "authToken": env["TWILIO_AUTH_TOKEN"],
                       "apiKeySid": "", "apiKeySecret": ""}
    if have("SLACK_BOT_TOKEN"):
        p["SLACK"] = {"accessToken": env["SLACK_BOT_TOKEN"], "signatureSecret": "", "notice": ""}
    if have("META_PAGE_ACCESS_TOKEN"):
        p["META"] = {"accessToken": env["META_PAGE_ACCESS_TOKEN"]}
    if have("HUBSPOT_APP_TOKEN"):
        p["HUBSPOT"] = {"appToken": env["HUBSPOT_APP_TOKEN"]}
    if have("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        secure = env.get("SMTP_SECURE", "true") == "true"
        p["SMTP"] = {"host": env["SMTP_HOST"], "port": int(env.get("SMTP_PORT", 465)), "user": env["SMTP_USER"],
                     "password": env["SMTP_PASSWORD"], "secure": secure}
        if not secure:
            p["SMTP"]["disableStartTls"] = False
    if have("N8N_API_KEY"):
        p["N8N_API"] = {"name": "X-N8N-API-KEY", "value": env["N8N_API_KEY"]}
    if have("VOICE_GATEWAY_KEY"):
        p["VOICE_KEY"] = {"name": "X-RoofRadar-Key", "value": env["VOICE_GATEWAY_KEY"]}
    if have("TWILIO_WEBHOOK_USER", "TWILIO_WEBHOOK_PASSWORD"):
        p["TWILIO_HOOK"] = {"user": env["TWILIO_WEBHOOK_USER"], "password": env["TWILIO_WEBHOOK_PASSWORD"]}
    return p


def main():
    dry = "--dry-run" in sys.argv
    activate = "--no-activate" not in sys.argv
    env = load_env()
    for k in ("N8N_URL", "N8N_API_KEY"):
        if not env.get(k):
            raise SystemExit(f"{k} is required (set it in .env)")
    api = N8N(env["N8N_URL"], env["N8N_API_KEY"], dry)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {"credentials": {}, "workflows": {}}

    # ------------------------------------------------------------ credentials
    print("Credentials")
    payloads = cred_payloads(env)
    cred_ids, missing = {}, []
    for key, (ctype, cname) in CREDS.items():
        cid = env.get(f"CRED_{key}_ID") or state["credentials"].get(key)
        if not cid and key in payloads:
            res = api.req("POST", "/credentials", {"name": cname, "type": ctype, "data": payloads[key]})
            cid = res["id"]
            print(f"  created  {cname:<32} {cid}")
        elif cid:
            print(f"  using    {cname:<32} {cid}")
        if cid:
            cred_ids[key] = cid
            state["credentials"][key] = cid
        else:
            missing.append(f"{cname} ({ctype}) → set CRED_{key}_ID")
    if missing:
        print("\n  ⚠ not configured yet (workflows import fine, but nodes using these stay red until set):")
        for m in missing:
            print("    -", m)

    # --------------------------------------------------------------- workflows
    manifest = json.load(open(os.path.join(WF_DIR, "manifest.json")))
    raw = {m["key"]: open(os.path.join(WF_DIR, m["file"])).read() for m in manifest}

    def render(key, ids):
        s = raw[key]
        s = re.sub(r"__WF__([0-9A-Z]+)__", lambda m: ids.get(m.group(1), m.group(0)), s)
        s = re.sub(r"__CRED_([A-Z_]+?)_ID__", lambda m: cred_ids.get(m.group(1), "UNSET"), s)
        wf = json.loads(s)
        wf.pop("meta", None)
        wf["name"] = wf["name"]
        return wf

    print("\nWorkflows (pass 1: create)")
    ids = dict(state["workflows"])
    for m in manifest:
        if m["key"] in ids:
            continue
        wf = render(m["key"], ids)
        body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
        body["settings"] = {k: v for k, v in body["settings"].items() if not str(v).startswith("__WF__")}
        res = api.req("POST", "/workflows", body)
        ids[m["key"]] = res["id"]
        print(f"  created  {m['name']:<40} {res['id']}")
    state["workflows"] = ids
    if not dry:
        json.dump(state, open(STATE, "w"), indent=2)

    print("\nWorkflows (pass 2: link ids + error workflow)")
    for m in manifest:
        wf = render(m["key"], ids)
        body = {k: wf[k] for k in ("name", "nodes", "connections", "settings")}
        api.req("PUT", f"/workflows/{ids[m['key']]}", body)
        print(f"  updated  {m['name']:<40} {ids[m['key']]}")

    if activate:
        print("\nActivation")
        for m in manifest:
            if not m["activate"]:
                continue
            api.req("POST", f"/workflows/{ids[m['key']]}/activate")
            print(f"  active   {m['name']}")

    print("\nWebhook URLs (configure these in Twilio / Meta / your website / the voice server):")
    base = env["N8N_URL"].rstrip("/")
    for m in manifest:
        for h in m["webhooks"]:
            method, path = h.split(" ", 1)
            print(f"  {m['key']:>3}  {method:<4} {base}{path}")
    print("\nDone.")


if __name__ == "__main__":
    main()
