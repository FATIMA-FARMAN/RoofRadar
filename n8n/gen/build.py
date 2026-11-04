"""Generate every RoofRadar n8n workflow into ../workflows/*.json

    python3 n8n/gen/build.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wf_intake, wf_ai, wf_booking, wf_business, wf_reliability, wf_voice, wf_recovery  # noqa: E402

BUILDERS = [
    ("01", wf_intake.wf01), ("02", wf_intake.wf02), ("03", wf_intake.wf03),
    ("04", wf_ai.wf04), ("05", wf_ai.wf05), ("06", wf_ai.wf06), ("07", wf_ai.wf07),
    ("08", wf_intake.wf08),
    ("09", wf_booking.wf09), ("10", wf_booking.wf10), ("11", wf_booking.wf11), ("12", wf_booking.wf12),
    ("13", wf_business.wf13), ("14", wf_business.wf14), ("15", wf_business.wf15), ("16", wf_business.wf16),
    ("17", wf_business.wf17), ("18", wf_business.wf18),
    ("19", wf_reliability.wf19), ("20", wf_reliability.wf20), ("21", wf_reliability.wf21),
    ("22", wf_reliability.wf22), ("23", wf_reliability.wf23),
    ("24A", wf_voice.wf24a), ("24B", wf_voice.wf24b), ("24C", wf_voice.wf24c),
    ("25", wf_recovery.wf25), ("26", wf_recovery.wf26),
]


def slug(name):
    return name.split("·", 1)[1].strip().lower().replace(" / ", "-").replace(" ", "-").replace("(", "").replace(")", "")


def main():
    out = os.path.join(HERE, "..", "workflows")
    os.makedirs(out, exist_ok=True)
    manifest = []
    for key, fn in BUILDERS:
        wf = fn()
        data = wf.export()
        fname = f"{key.lower()}-{slug(wf.name)}.json"
        with open(os.path.join(out, fname), "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        triggers = [n["type"].split(".")[-1] for n in data["nodes"]
                    if n["type"].split(".")[-1] in ("webhook", "scheduleTrigger", "errorTrigger", "executeWorkflowTrigger")]
        webhooks = [f'{n["parameters"].get("httpMethod","POST")} /webhook/{n["parameters"]["path"]}'
                    for n in data["nodes"] if n["type"].endswith(".webhook")]
        manifest.append({"key": key, "name": wf.name, "file": fname, "nodes": len(data["nodes"]),
                         "activate": any(t in ("webhook", "scheduleTrigger") for t in triggers),
                         "triggers": sorted(set(triggers)), "webhooks": webhooks})
        print(f"{key:>3}  {len(data['nodes']):>3} nodes  {fname}")
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"\n{len(manifest)} workflows written to n8n/workflows/")


if __name__ == "__main__":
    main()
