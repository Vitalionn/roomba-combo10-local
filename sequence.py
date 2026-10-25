"""Run cleaning jobs back to back: the next one starts only when the previous one is finished (robot back on
the dock, bin emptied, notReady 0). Usage: python3 sequence.py  (edit STEPS at the bottom). Logs to data/sequence.log.
Set ROOMBA_APP to the web app's address (default http://127.0.0.1:8083)."""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

B = os.environ.get("ROOMBA_APP", "http://127.0.0.1:8083")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "sequence.log")


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def get(p):
    return json.load(urllib.request.urlopen(B + p, timeout=15))


def post(p, body):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(r, timeout=180))
    except urllib.error.HTTPError as e:
        return json.load(e)


def status():
    s = get("/api/raw"); c = s.get("cleanMissionStatus") or {}
    return c.get("phase"), c.get("cycle"), c.get("notReady"), c.get("error"), s.get("batPct")


def wait_until_done(max_hours=3):
    """Done = cycle none, phase charge, notReady 0, stable for 30 s. An error (error != 0) aborts."""
    t0 = time.time(); last = None; stable_since = None
    while time.time() - t0 < max_hours * 3600:
        ph, cy, nr, err, bat = status()
        if (ph, cy, nr, err) != last:
            log(f"  status {ph} {cy} notReady {nr} error {err} battery {bat}")
            last = (ph, cy, nr, err)
        if err:
            log(f"  ERROR {err} on the robot; sequence aborted"); return False
        if ph == "charge" and cy == "none" and nr == 0:
            stable_since = stable_since or time.time()
            if time.time() - stable_since >= 30:
                return True
        else:
            stable_since = None
        time.sleep(10)
    log("  time is up; sequence aborted"); return False


def step(name, body):
    log(f"START {name}: {json.dumps(body, ensure_ascii=False)}")
    for attempt in range(1, 4):
        r = post("/api/start", body)
        if r.get("ok"):
            log(f"  accepted after {r.get('attempts')} attempt(s)"); return True
        log(f"  not accepted ({r.get('error')}); attempt {attempt}/3, waiting 60 s")
        time.sleep(60)
    return False


STEPS = [
    ("kitchen vacuum only", {"regions": ["4"], "mode": "vacuum", "suction": "normal", "passes": "1"}),
    ("whole house vacuum + mop", {"regions": [], "mode": "vacuum+mop", "suction": "normal", "water": "2",
                                  "scrub": False, "passes": "1"}),
]

if __name__ == "__main__":
    log(f"=== sequence of {len(STEPS)} steps (pid {os.getpid()})")
    for i, (name, body) in enumerate(STEPS, 1):
        if i > 1:
            log("waiting for the previous run to finish")
            if not wait_until_done():
                sys.exit(1)
            time.sleep(20)
        if not step(name, body):
            log("sequence stopped"); sys.exit(1)
        time.sleep(60)  # let it run for a bit before watching for 'done'
    log("waiting for the last run to finish")
    wait_until_done()
    log("=== sequence finished")
