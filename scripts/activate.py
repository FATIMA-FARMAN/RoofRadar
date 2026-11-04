#!/usr/bin/env python3
"""Publish (activate) specific RoofRadar workflows by key.

    python3 scripts/activate.py 01 03 17 18 23      # re-publish these after an update
    python3 scripts/activate.py 25 26               # switch on the new schedules (after the Twilio upgrade)
"""
import json, sys, urllib.request, urllib.error

env = {}
for l in open('.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); env.setdefault(k.strip(), v.strip())
base = env['N8N_URL'].rstrip('/') + '/api/v1'
ids = json.load(open('.deploy-state.json'))['workflows']
names = {m['key']: m['name'] for m in json.load(open('n8n/workflows/manifest.json'))}
for k in sys.argv[1:]:
    k = k.upper()
    if k not in ids:
        print('SKIP      ', k, '(not deployed yet: run scripts/deploy.py first)'); continue
    r = urllib.request.Request(f"{base}/workflows/{ids[k]}/activate", data=b"{}", method="POST",
        headers={"X-N8N-API-KEY": env['N8N_API_KEY'], "Content-Type": "application/json", "Accept": "application/json"})
    try:
        urllib.request.urlopen(r, timeout=60); print('published ', names.get(k, k))
    except urllib.error.HTTPError as e:
        print('FAILED    ', names.get(k, k), '->', e.read().decode()[:300])
