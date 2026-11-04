#!/usr/bin/env python3
"""Static checks for the generated n8n workflows.

  python3 scripts/validate_workflows.py                 # graph + reference checks
  python3 scripts/validate_workflows.py --sql "$DATABASE_URL"   # also PREPARE every SQL query against Postgres

Checks:
  * every connection points at an existing node
  * every $('NODE') reference in any parameter names a node in the same workflow
  * every __WF__KEY__ placeholder names a real workflow
  * every Execute Workflow target is a workflow with an Execute Workflow Trigger
  * every node is reachable from a trigger (sticky notes excepted)
  * webhook paths are unique across the system
  * (--sql) every Postgres query parses and type-checks (PREPARE, nothing is executed)
"""
import json
import os
import re
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WF_DIR = os.path.join(ROOT, "n8n", "workflows")


def load():
    manifest = json.load(open(os.path.join(WF_DIR, "manifest.json")))
    return {m["key"]: json.load(open(os.path.join(WF_DIR, m["file"]))) for m in manifest}


def walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)


def main():
    wfs = load()
    errors, paths = [], {}
    sub_targets = {k for k, w in wfs.items() if any(n["type"].endswith("executeWorkflowTrigger") for n in w["nodes"])}
    for key, w in wfs.items():
        names = {n["name"] for n in w["nodes"]}
        # connections
        for src, c in w["connections"].items():
            if src not in names:
                errors.append(f"{key}: connection from missing node {src}")
            for outs in c["main"]:
                for e in outs:
                    if e["node"] not in names:
                        errors.append(f"{key}: connection to missing node {e['node']}")
        # references
        for n in w["nodes"]:
            for s in walk_strings(n["parameters"]):
                for ref in re.findall(r"\$\('([^']+)'\)", s):
                    if ref not in names:
                        errors.append(f"{key}/{n['name']}: references unknown node $('{ref}')")
                for tgt in re.findall(r"__WF__([0-9A-Z]+)__", s):
                    if tgt not in wfs:
                        errors.append(f"{key}/{n['name']}: unknown workflow placeholder {tgt}")
                    elif n["type"].endswith("executeWorkflow") and tgt not in sub_targets:
                        errors.append(f"{key}/{n['name']}: target {tgt} has no Execute Workflow Trigger")
            if n["type"].endswith(".webhook"):
                p = (n["parameters"].get("httpMethod", "POST"), n["parameters"]["path"])
                if p in paths:
                    errors.append(f"{key}: webhook {p} duplicates {paths[p]}")
                paths[p] = key
        # reachability
        incoming = {n["name"]: 0 for n in w["nodes"]}
        for src, c in w["connections"].items():
            for outs in c["main"]:
                for e in outs:
                    incoming[e["node"]] += 1
        roots = [n["name"] for n in w["nodes"] if incoming[n["name"]] == 0 and not n["type"].endswith("stickyNote")]
        for r in roots:
            t = next(n["type"] for n in w["nodes"] if n["name"] == r)
            if not re.search(r"(webhook|Trigger|errorTrigger)$", t):
                errors.append(f"{key}: node '{r}' is not connected to any trigger")
    print(f"checked {len(wfs)} workflows, {sum(len(w['nodes']) for w in wfs.values())} nodes, {len(paths)} webhooks")

    if "--sql" in sys.argv:
        dsn = sys.argv[sys.argv.index("--sql") + 1]
        n_ok = 0
        for key, w in wfs.items():
            for n in w["nodes"]:
                if not n["type"].endswith(".postgres"):
                    continue
                q = n["parameters"]["query"].strip().rstrip(";")
                sql = f"PREPARE rr_check AS {q};\nDEALLOCATE rr_check;"
                r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    errors.append(f"{key}/{n['name']}: SQL error: {r.stderr.strip().splitlines()[0]}")
                else:
                    n_ok += 1
        print(f"prepared {n_ok} SQL statements successfully")

    if errors:
        print("\nFAILED:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    print("OK — no problems found")


if __name__ == "__main__":
    main()
