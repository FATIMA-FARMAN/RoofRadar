import json, urllib.request, urllib.error
env = {}
for l in open('.env'):
    l = l.strip()
    if l and not l.startswith('#') and '=' in l:
        k, v = l.split('=', 1); env.setdefault(k.strip(), v.strip())
base = env['N8N_URL'].rstrip('/') + '/api/v1'; key = env['N8N_API_KEY']
ids = json.load(open('.deploy-state.json'))['workflows']
man = {m['key']: m for m in json.load(open('n8n/workflows/manifest.json'))}
todo = [k for k, m in man.items() if not m['activate'] or k == '01']
def publish(k):
    r = urllib.request.Request(f"{base}/workflows/{ids[k]}/activate", data=b"{}", method="POST",
        headers={"X-N8N-API-KEY": key, "Content-Type": "application/json", "Accept": "application/json"})
    try: urllib.request.urlopen(r, timeout=60); return None
    except urllib.error.HTTPError as e: return e.read().decode()[:300]
errors = {}
for _ in range(8):
    left = []
    for k in todo:
        err = publish(k)
        if err: left.append(k); errors[k] = err
        else: print("published ", man[k]['name'])
    if len(left) in (0, len(todo)): todo = left; break
    todo = left
for k in todo: print("FAILED    ", man[k]['name'], "->", errors[k])
print("done")
