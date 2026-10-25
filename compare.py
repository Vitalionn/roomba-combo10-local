#!/usr/bin/env python3
"""Compare the last per-room start command sent by the iRobot app (appCommand) with the last one sent by our app (command)."""
import json
import sys

ev = [json.loads(l) for l in open("data/events.jsonl")]
app = [e for e in ev if e["type"] == "appCommand" and '"regions": [' in e["text"]]
ours = [e for e in ev if e["type"] == "command" and '"regions": [' in e["text"]]
print(f"app room commands logged: {len(app)}, ours: {len(ours)}")
if not app:
    # fallback: look in the dumps of explore.py or in the raw logs
    print("no appCommand with regions in events.jsonl")
    sys.exit(0)
a = json.loads(app[-1]["text"])
o = json.loads(ours[-1]["text"]) if ours else {}
print("APP:", json.dumps(a, sort_keys=True)[:1500])
print("OURS:", json.dumps(o, sort_keys=True)[:1500])
for k in sorted(set(a) | set(o)):
    if k in ("regions", "time"):
        continue
    if a.get(k) != o.get(k):
        print("DIFFERENCE", k, repr(a.get(k)), "vs", repr(o.get(k)))
if o.get("regions"):
    ra, ro = a["regions"][0], o["regions"][0]
    for k in sorted(set(ra) | set(ro)):
        if ra.get(k) != ro.get(k):
            print("DIFFERENCE region", k, repr(ra.get(k)), "vs", repr(ro.get(k)))
