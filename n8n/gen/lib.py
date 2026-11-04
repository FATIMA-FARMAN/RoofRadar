"""
Tiny builder for n8n workflow JSON.

Every workflow is built in Python so the 24 canvases share one set of
conventions (node types/versions, credential placeholders, error workflow,
timezone) and can be regenerated after a change instead of hand-edited.

Placeholders resolved at deploy time by scripts/deploy.py:
  __WF__<KEY>__          -> id of another RoofRadar workflow
  __CRED_<KEY>_ID__      -> id of an n8n credential you created
"""
import json
import uuid

ORG_DEFAULT = "00000000-0000-4000-8000-000000000001"

CREDS = {
    "POSTGRES":   ("postgres",                 "RoofRadar Postgres"),
    "OPENAI":     ("openAiApi",                "RoofRadar OpenAI"),
    "TWILIO":     ("twilioApi",                "RoofRadar Twilio"),
    "SLACK":      ("slackApi",                 "RoofRadar Slack"),
    "GCAL":       ("googleCalendarOAuth2Api",  "RoofRadar Google Calendar"),
    "META":       ("facebookGraphApi",         "RoofRadar Meta"),
    "HUBSPOT":    ("hubspotAppToken",          "RoofRadar HubSpot"),
    "SMTP":       ("smtp",                     "RoofRadar SMTP"),
    "N8N_API":    ("httpHeaderAuth",           "RoofRadar n8n API"),
    "VOICE_KEY":  ("httpHeaderAuth",           "RoofRadar Voice Gateway Key"),
    "TWILIO_HOOK": ("httpBasicAuth",           "RoofRadar Twilio Webhook Auth"),
}


def cred(key):
    ctype, cname = CREDS[key]
    return {ctype: {"id": f"__CRED_{key}_ID__", "name": cname}}


def wf_ref(key):
    return {"__rl": True, "mode": "id", "value": f"__WF__{key}__"}


def _uid():
    return str(uuid.uuid4())


class WF:
    def __init__(self, key, name, description="", timezone="America/New_York", error_workflow=True):
        self.key = key
        self.name = name
        self.description = description
        self.nodes = []
        self.connections = {}
        self.timezone = timezone
        self.error_workflow = error_workflow
        self._names = set()
        self.gates = {}   # sql node -> filter node that drops Postgres' {success:true} placeholder item

    # ---------------------------------------------------------------- core
    def add(self, name, ntype, version, params, creds=None, **extra):
        assert name not in self._names, f"duplicate node name {name} in {self.name}"
        self._names.add(name)
        node = {
            "parameters": params,
            "id": _uid(),
            "name": name,
            "type": ntype,
            "typeVersion": version,
            "position": [0, 0],
        }
        if creds:
            c = {}
            for k in creds:
                c.update(cred(k))
            node["credentials"] = c
        node.update(extra)
        self.nodes.append(node)
        return name

    def connect(self, src, dst, out=0, inp=0):
        src = self.gates.get(src, src)
        outs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outs) <= out:
            outs.append([])
        outs[out].append({"node": dst, "type": "main", "index": inp})

    def chain(self, *names):
        for a, b in zip(names, names[1:]):
            self.connect(a, b)
        return names[-1]

    # ------------------------------------------------------------ triggers
    def webhook(self, name, path, method="POST", respond="onReceived", auth=None):
        params = {"httpMethod": method, "path": path, "options": {}}
        if respond == "responseNode":
            params["responseMode"] = "responseNode"
        elif respond == "lastNode":
            params["responseMode"] = "lastNode"
        creds = None
        if auth == "basic":
            params["authentication"] = "basicAuth"
            creds = ["TWILIO_HOOK"]
        elif auth == "header":
            params["authentication"] = "headerAuth"
            creds = ["VOICE_KEY"]
        return self.add(name, "n8n-nodes-base.webhook", 2, params, creds, webhookId=_uid())

    def sub_trigger(self, name="EXECUTE_WORKFLOW_TRIGGER"):
        return self.add(name, "n8n-nodes-base.executeWorkflowTrigger", 1, {})

    def schedule(self, name, minutes=None, cron=None):
        if cron:
            rule = {"interval": [{"field": "cronExpression", "expression": cron}]}
        else:
            rule = {"interval": [{"field": "minutes", "minutesInterval": minutes}]}
        return self.add(name, "n8n-nodes-base.scheduleTrigger", 1.2, {"rule": rule})

    def error_trigger(self, name="ERROR_TRIGGER"):
        return self.add(name, "n8n-nodes-base.errorTrigger", 1, {})

    # --------------------------------------------------------------- nodes
    def code(self, name, js, each=False, **extra):
        params = {"jsCode": js.strip("\n")}
        if each:
            params["mode"] = "runOnceForEachItem"
        return self.add(name, "n8n-nodes-base.code", 2, params, **extra)

    def sql(self, name, query, args=None, always=False, on_error=None, gate=None, **extra):
        """Postgres executeQuery. args = list of JS expressions -> $1..$n.
        gate=<column>: n8n's Postgres node emits {success:true} when a non-SELECT statement returns no rows;
        a Filter node is inserted so the flow stops unless the row really has <column>."""
        opts = {}
        if args:
            opts["queryReplacement"] = "={{ [ " + ", ".join(args) + " ] }}"
        params = {"operation": "executeQuery", "query": query.strip("\n"), "options": opts}
        kw = dict(extra)
        if always:
            kw["alwaysOutputData"] = True
        if on_error:
            kw["onError"] = on_error
        self.add(name, "n8n-nodes-base.postgres", 2.5, params, ["POSTGRES"], **kw)
        if gate:
            f = self.filter(f"{name}_HAS_ROWS", f"$json.{gate} !== undefined && $json.{gate} !== null")
            outs = self.connections.setdefault(name, {"main": []})["main"]
            outs.append([{"node": f, "type": "main", "index": 0}])
            self.gates[name] = f
        return name

    def filter(self, name, expr):
        params = {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [{
                    "id": _uid(),
                    "leftValue": "={{ " + expr + " }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        }
        return self.add(name, "n8n-nodes-base.filter", 2, params)

    def if_(self, name, expr):
        """IF node: true branch = output 0, false branch = output 1."""
        params = {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
                "conditions": [{
                    "id": _uid(),
                    "leftValue": "={{ " + expr + " }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "options": {},
        }
        return self.add(name, "n8n-nodes-base.if", 2, params)

    def switch(self, name, expr, keys):
        """Switch on a string expression. Output i = keys[i]; last output = fallback."""
        rules = []
        for k in keys:
            rules.append({
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                    "conditions": [{
                        "id": _uid(),
                        "leftValue": "={{ " + expr + " }}",
                        "rightValue": k,
                        "operator": {"type": "string", "operation": "equals"},
                    }],
                    "combinator": "and",
                },
                "renameOutput": True,
                "outputKey": k,
            })
        params = {"rules": {"values": rules}, "options": {"fallbackOutput": "extra", "renameFallbackOutput": "OTHER"}}
        return self.add(name, "n8n-nodes-base.switch", 3, params)

    def call(self, name, wf_key, wait=True, **extra):
        """Execute another RoofRadar workflow once per incoming item."""
        params = {"source": "database", "workflowId": wf_ref(wf_key), "mode": "each",
                  "options": {} if wait else {"waitForSubWorkflow": False}}
        return self.add(name, "n8n-nodes-base.executeWorkflow", 1.1, params, **extra)

    def http(self, name, method, url, body_expr=None, cred_key=None, cred_type=None,
             query=None, full_response=False, never_error=False, retry=True, **extra):
        params = {"method": method, "url": url, "options": {}}
        creds = None
        if cred_key:
            params["authentication"] = "predefinedCredentialType"
            params["nodeCredentialType"] = cred_type or CREDS[cred_key][0]
            creds = [cred_key]
        if query:
            params["sendQuery"] = True
            params["queryParameters"] = {"parameters": [{"name": k, "value": v} for k, v in query.items()]}
        if body_expr is not None:
            params["sendBody"] = True
            params["specifyBody"] = "json"
            params["jsonBody"] = body_expr
        if full_response or never_error:
            params["options"]["response"] = {"response": {
                "fullResponse": full_response, "neverError": never_error}}
        params["options"]["timeout"] = 20000
        kw = dict(extra)
        if retry:
            kw.setdefault("retryOnFail", True)
            kw.setdefault("maxTries", 3)
            kw.setdefault("waitBetweenTries", 2000)
        return self.add(name, "n8n-nodes-base.httpRequest", 4.2, params, creds, **kw)

    def openai(self, name, **extra):
        """Chat Completions call; expects $json.openai_request built by the previous Code node."""
        return self.http(
            name, "POST", "https://api.openai.com/v1/chat/completions",
            body_expr="={{ JSON.stringify($json.openai_request) }}",
            cred_key="OPENAI", **extra)

    def twilio_sms(self, name, from_expr, to_expr, body_expr, whatsapp_expr="false", **extra):
        params = {
            "from": "={{ " + from_expr + " }}",
            "to": "={{ " + to_expr + " }}",
            "toWhatsapp": "={{ " + whatsapp_expr + " }}",
            "message": "={{ " + body_expr + " }}",
            "options": {},
        }
        kw = dict(extra)
        kw.setdefault("retryOnFail", True)
        kw.setdefault("maxTries", 2)
        kw.setdefault("waitBetweenTries", 3000)
        return self.add(name, "n8n-nodes-base.twilio", 1, params, ["TWILIO"], **kw)

    def slack(self, name, channel_expr, text_expr, **extra):
        params = {
            "select": "channel",
            "channelId": {"__rl": True, "mode": "name", "value": "={{ " + channel_expr + " }}"},
            "text": "={{ " + text_expr + " }}",
            "otherOptions": {"includeLinkToWorkflow": False},
        }
        kw = dict(extra)
        kw.setdefault("onError", "continueRegularOutput")  # an alert failure must never break the lead flow
        return self.add(name, "n8n-nodes-base.slack", 2.2, params, ["SLACK"], **kw)

    def respond(self, name, body_expr, content_type="application/json", code=200):
        if content_type == "application/json":
            params = {"respondWith": "json", "responseBody": body_expr, "options": {"responseCode": code}}
        else:
            params = {"respondWith": "text", "responseBody": body_expr,
                      "options": {"responseCode": code, "responseHeaders": {
                          "entries": [{"name": "Content-Type", "value": content_type}]}}}
        return self.add(name, "n8n-nodes-base.respondToWebhook", 1.1, params)

    def noop(self, name):
        return self.add(name, "n8n-nodes-base.noOp", 1, {})

    def note(self, text, width=520, height=260):
        name = f"Note {len([n for n in self.nodes if n['type'].endswith('stickyNote')]) + 1}"
        return self.add(name, "n8n-nodes-base.stickyNote", 1,
                        {"content": text.strip(), "width": width, "height": height})

    # -------------------------------------------------------------- export
    def _layout(self):
        by_name = {n["name"]: n for n in self.nodes}
        incoming = {n: 0 for n in by_name}
        for src, c in self.connections.items():
            for outs in c["main"]:
                for e in outs:
                    incoming[e["node"]] += 1
        depth = {}
        roots = [n for n, k in incoming.items() if k == 0 and not by_name[n]["type"].endswith("stickyNote")]
        frontier = [(r, 0) for r in roots]
        while frontier:
            nxt = []
            for n, d in frontier:
                if depth.get(n, -1) >= d or d > 60:
                    continue
                depth[n] = d
                for outs in self.connections.get(n, {"main": []})["main"]:
                    for e in outs:
                        nxt.append((e["node"], d + 1))
            frontier = nxt
        rows = {}
        for n in self.nodes:
            if n["type"].endswith("stickyNote"):
                continue
            d = depth.get(n["name"], 0)
            r = rows.get(d, 0)
            rows[d] = r + 1
            n["position"] = [240 + d * 260, 300 + r * 180]
        notes = [n for n in self.nodes if n["type"].endswith("stickyNote")]
        for i, n in enumerate(notes):
            n["position"] = [240 + i * 560, -60]

    def validate(self):
        names = {n["name"] for n in self.nodes}
        for src, c in self.connections.items():
            assert src in names, f"{self.name}: connection from unknown node {src}"
            for outs in c["main"]:
                for e in outs:
                    assert e["node"] in names, f"{self.name}: connection to unknown node {e['node']}"
        return True

    def export(self):
        self.validate()
        self._layout()
        settings = {"executionOrder": "v1", "timezone": self.timezone,
                    "saveDataErrorExecution": "all", "saveDataSuccessExecution": "all",
                    "saveManualExecutions": True, "callerPolicy": "workflowsFromSameOwner"}
        if self.error_workflow:
            settings["errorWorkflow"] = "__WF__19__"
        return {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": settings,
            "meta": {"roofradar_key": self.key, "description": self.description},
        }


def js_str(s):
    """Embed a Python string as a JS string literal."""
    return json.dumps(s)
