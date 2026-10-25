"""Bare local control for the Roomba. Flask + MQTT bridge, no cloud."""
import json
import os
import time
from datetime import date, datetime

from flask import Flask, jsonify, render_template, request

from bridge import Bridge, dig

DATA = os.environ.get("ROOMBA_DATA", "/app/data")
os.makedirs(DATA, exist_ok=True)
MAINTENANCE = os.path.join(DATA, "maintenance.json")
ROOMS = os.path.join(DATA, "rooms.json")

app = Flask(__name__)
bridge = Bridge(os.environ["ROOMBA_IP"], os.environ["ROOMBA_BLID"], os.environ["ROOMBA_PASSWORD"], DATA)
bridge.start()

# ---- translation table: app setting -> robot parameters (measured 2026-10-24, see README) ----
SUCTION = {"eco": (False, False), "normal": (True, False), "high": (False, True)}  # carpetBoost, vacHigh
PASSES = {"1": (True, False), "2": (False, True), "auto": (False, False)}         # noAutoPasses, twoPass
MODE = {"vacuum": 2, "mop": 4, "vacuum+mop": 6}                                   # operatingMode (measured)

# Preferences that may be set through 'delta', with their allowed values
PREFS = {
    "imgUpload": {0, 1},
    "mapUploadAllowed": {True, False},
    "pmapLearningAllowed": {True, False},
    "padWashAllowed": {0, 1},
    "padDryAllowed": {0, 1},
    "binPause": {True, False},
    "carpetBoost": {True, False},
    "vacHigh": {True, False},
    "twoPass": {True, False},
    "noAutoPasses": {True, False},
    "childLock": {True, False},
}

# Maintenance: iRobot guidelines as a starting point; intervals can be overridden in maintenance.json
PARTS_DEFAULT = {
    "filter": {"name": "Filter", "interval_days": 60, "counter": "nMssn", "interval_counter": None},
    "rollers": {"name": "Rubber rollers", "interval_days": 270, "counter": "nMssn", "interval_counter": None},
    "side_brush": {"name": "Side brush", "interval_days": 365, "counter": "nMssn", "interval_counter": None},
    "pad": {"name": "Mop pad", "interval_days": None, "counter": "nScrubs", "interval_counter": 30},
    "bag": {"name": "Dock dust bag", "interval_days": None, "counter": "nEvacs", "interval_counter": 60},
}


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def counters(state):
    return {
        "nMssn": dig(state, "bbmssn", "nMssn"),
        "nMssnF": dig(state, "bbmssn", "nMssnF"),
        "nMssnC": dig(state, "bbmssn", "nMssnC"),
        "nMssnOk": dig(state, "bbmssn", "nMssnOk"),
        "nEvacs": dig(state, "bbrun", "nEvacs"),
        "nScrubs": dig(state, "bbrun", "nScrubs"),
        "nStuck": dig(state, "bbrun", "nStuck"),
        "nMBStll": dig(state, "bbrun", "nMBStll"),
    }


def maintenance_overview(state):
    cfg = load_json(MAINTENANCE, {})
    now = counters(state)
    out = []
    for key, d in PARTS_DEFAULT.items():
        rec = {**d, **cfg.get(key, {})}
        replaced = rec.get("replaced_on")
        counter_at = rec.get("counter_at")
        counter_now = now.get(rec["counter"])
        days = (date.today() - date.fromisoformat(replaced)).days if replaced else None
        since = (counter_now - counter_at) if (counter_now is not None and counter_at is not None) else None
        status = "unknown"
        if replaced:
            status = "ok"
            if rec.get("interval_days") and days is not None and days >= rec["interval_days"]:
                status = "replace"
            if rec.get("interval_counter") and since is not None and since >= rec["interval_counter"]:
                status = "replace"
        out.append({"key": key, "name": rec["name"], "replaced_on": replaced, "days": days,
                    "counter": rec["counter"], "since": since, "interval_days": rec.get("interval_days"),
                    "interval_counter": rec.get("interval_counter"), "status": status})
    return out


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    s = bridge.snapshot()
    cms = s.get("cleanMissionStatus", {})
    dock = s.get("dock", {})
    return jsonify({
        "connected": bridge.connected,
        "connection_error": bridge.last_error,
        "last_message": bridge.last_msg,
        "name": s.get("name"),
        "battery": s.get("batPct"),
        "phase": cms.get("phase"), "cycle": cms.get("cycle"), "error": cms.get("error"),
        "notReady": cms.get("notReady"), "missionMin": cms.get("mssnM"), "operatingMode": cms.get("operatingMode"),
        "bin": s.get("bin"),
        "robotTank": {"level": s.get("tankLvl"), "present": s.get("tankPresent")},
        "pad": s.get("detectedPad"),
        "dock": {"water": dock.get("tankLvl"), "wasteWater": dock.get("gwTankLvl"), "error": dock.get("error"),
                 "state": dock.get("state"), "pwState": dock.get("pwState"), "pdState": dock.get("pdState"),
                 "frState": dock.get("frState")},
        "prefs": {k: s.get(k) for k in PREFS},
        "padWetness": s.get("padWetness"),
        "counters": counters(s),
        "lastCommand": s.get("lastCommand"),
        "maintenance": maintenance_overview(s),
        "rooms": load_json(ROOMS, {}),
        "events": bridge.events(25),
        "schedule": s.get("cleanSchedule2"),
    })


@app.get("/api/raw")
def api_raw():
    return jsonify(bridge.snapshot())


@app.get("/api/recent")
def api_recent():
    """Most recent raw MQTT messages (all topics), newest first. For diagnosing rejected commands."""
    n = int(request.args.get("n", 40))
    return jsonify(list(bridge.recent)[-n:][::-1])


def start_params(body, per_room=False):
    """Builds the params block EXACTLY as the iRobot app sends it (measured 2026-10-24). Any deviation = silently ignored.
    Two different field sets:
      whole house: carpetBoost, noAutoPasses, operatingMode, padWetness, swScrub, twoPass, vacHigh
      per room   : carpetBoost, gentleMode, noAutoPasses, operatingMode, padWashAfter, twoPass, vacHigh
                   + padWetness, swScrub ONLY in a mop mode (operatingMode 4/6)."""
    cb, vh = SUCTION[body.get("suction", "normal")]
    nap, tp = PASSES[str(body.get("passes", "1"))]
    water = int(body.get("water", 2))
    if water not in (1, 2, 3):
        water = 2
    mode = MODE[body.get("mode", "vacuum")]
    mop = mode in (4, 6)
    if per_room:
        p = {"carpetBoost": cb, "gentleMode": 0, "noAutoPasses": nap, "operatingMode": mode,
             "padWashAfter": 1 if body.get("padWashAfter") else 0, "twoPass": tp, "vacHigh": vh}
        if mop:
            p["padWetness"] = {"disposable": water, "reusable": water}
            p["swScrub"] = 1 if body.get("scrub") else 0
    else:
        p = {"carpetBoost": cb, "noAutoPasses": nap, "operatingMode": mode,
             "padWetness": {"disposable": water, "reusable": water},
             "swScrub": 1 if body.get("scrub") else 0, "twoPass": tp, "vacHigh": vh}
    return p


@app.post("/api/start")
def api_start():
    body = request.get_json(force=True) or {}
    regions = body.get("regions") or []
    params = start_params(body, per_room=bool(regions))
    rooms = load_json(ROOMS, {})
    if regions:
        # Format as the iRobot app sends it, BUT without top-level params/favorite_id: lastCommand is a normalized
        # echo that shows those keys as null; send them as null yourself and the robot silently drops the whole
        # command (measured 2026-10-24; before that not a single per-room start from this app was accepted).
        if not rooms.get("pmap_id"):
            return jsonify({"ok": False, "error": "no map ids known; import the rooms first"}), 400
        extra = {"ordered": 1, "pmap_id": rooms["pmap_id"], "user_pmapv_id": rooms.get("user_pmapv_id"),
                 "regions": [{"region_id": str(r), "type": "rid", "params": dict(params)} for r in regions]}
    else:
        extra = {"params": params}
    cms = bridge.snapshot().get("cleanMissionStatus") or {}
    if cms.get("cycle") in ("clean", "dock"):  # running or paused mission: a start is ignored anyway (or becomes a resume)
        return jsonify({"ok": False, "error": f"robot is busy (cycle {cms.get('cycle')}, phase {cms.get('phase')}); stop or resume first"}), 409
    # No stop beforehand: a stop puts the robot on notReady 39/23 for ~12 s, during which a start is ignored.
    # Success = the robot echoes OUR command in lastCommand (same time). On the notReady blip (23/68, internal
    # readiness check) retry twice with a pause.
    started = False
    payload = None
    for attempt in range(1, 4):
        ok, payload = bridge.command("start", extra)
        for _ in range(24):  # wait ~12 s
            time.sleep(0.5)
            s = bridge.snapshot()
            lc = s.get("lastCommand") or {}
            # Success = own time back in lastCommand and an active cycle. Do NOT wait for phase=run: a mop mission
            # starts on the dock with refill/padWash and only reaches run later (false negative 2026-10-24).
            cms_now = s.get("cleanMissionStatus") or {}
            if lc.get("time") == payload["time"] and cms_now.get("cycle") == "clean" and cms_now.get("phase") != "charge":
                started = True
                break
        if started:
            break
        time.sleep(3)
    bridge.event("startResult", f"started={started} after {attempt} attempt(s)")
    return jsonify({"ok": started, "started": started, "attempts": attempt, "sent": payload,
                    "error": None if started else "robot did not accept the start after 3 attempts; check notReady in the status"})


@app.post("/api/cmd")
def api_cmd():
    body = request.get_json(force=True) or {}
    c = body.get("command")
    if c not in ("stop", "pause", "resume", "dock", "find", "evac", "train"):
        return jsonify({"ok": False, "error": "unknown command"}), 400
    if c == "dock" and (bridge.snapshot().get("cleanMissionStatus") or {}).get("phase") == "run":
        bridge.command("stop")  # dock is ignored while driving; stop first (measured 2026-10-24)
        time.sleep(3)
    ok, payload = bridge.command(c)
    return jsonify({"ok": ok, "sent": payload})


@app.post("/api/raw_cmd")
def api_raw_cmd():
    """Experimental: publish an arbitrary command payload (LAN only). For probing what the robot accepts."""
    body = request.get_json(force=True) or {}
    if not isinstance(body.get("command"), str):
        return jsonify({"ok": False, "error": "command missing"}), 400
    extra = {k: v for k, v in body.items() if k != "command"}
    ok, payload = bridge.command(body["command"], extra)
    return jsonify({"ok": ok, "sent": payload})


@app.post("/api/rooms/name")
def api_rooms_name():
    body = request.get_json(force=True) or {}
    rooms = load_json(ROOMS, {})
    rid, name = str(body.get("id", "")), (body.get("name") or "").strip()
    for r in rooms.get("regions", []):
        if r["id"] == rid:
            r["name"] = name or rid
            save_json(ROOMS, rooms)
            return jsonify({"ok": True, "sent": f"room {rid} is now called {r['name']}"})
    return jsonify({"ok": False, "error": f"room {rid} unknown"}), 400


@app.post("/api/prefs")
def api_prefs():
    body = request.get_json(force=True) or {}
    prefs = {}
    for k, v in body.items():
        if k not in PREFS:
            return jsonify({"ok": False, "error": f"{k} not allowed"}), 400
        if v not in PREFS[k]:
            return jsonify({"ok": False, "error": f"{k}: value {v!r} not allowed"}), 400
        prefs[k] = v
    ok, payload = bridge.set_prefs(prefs)
    return jsonify({"ok": ok, "sent": payload, "note": "check whether the robot adopts the value (status refreshes)"})


@app.post("/api/bridge/pause")
def api_bridge_pause():
    """Release the bridge so the iRobot app (on the same Wi-Fi) can connect locally; afterwards we capture lastCommand."""
    body = request.get_json(force=True) or {}
    minutes = max(1, min(60, int(body.get("minutes", 10))))
    bridge.pause(minutes)
    return jsonify({"ok": True, "sent": f"bridge released for {minutes} min"})


@app.post("/api/bridge/resume")
def api_bridge_resume():
    bridge.resume()
    return jsonify({"ok": True, "sent": "bridge reconnecting"})


@app.post("/api/rooms/import")
def api_rooms_import():
    """Import room ids from the last command the iRobot app sent (a start with rooms ticked).
    The cloud map API refuses (403); this is the reliable route: the robot reports the command itself."""
    lc = bridge.snapshot().get("lastCommand") or {}
    regs = lc.get("regions") or []
    if not lc.get("pmap_id") or not regs:
        return jsonify({"ok": False, "error": f"last command ({lc.get('command')}) contains no rooms. In the iRobot app start "
                                              "a run with SOME of the rooms ticked, close the iRobot app WITHOUT stopping "
                                              "(stopping overwrites the command), wait until the bridge is connected and click again; "
                                              "then stop the robot from this app"}), 400
    existing = {r["id"]: r for r in load_json(ROOMS, {}).get("regions", [])}
    for r in regs:
        rid = str(r.get("region_id"))
        existing[rid] = {"id": rid, "name": r.get("region_name") or existing.get(rid, {}).get("name") or rid,
                         "type": r.get("region_type") or existing.get(rid, {}).get("type")}
    rooms = {"pmap_id": lc["pmap_id"], "user_pmapv_id": lc.get("user_pmapv_id"),
             "regions": sorted(existing.values(), key=lambda r: r["name"] or ""),
             "source": f"lastCommand {datetime.now():%Y-%m-%d %H:%M}"}
    save_json(ROOMS, rooms)
    bridge.event("rooms", f"{len(regs)} rooms imported from lastCommand")
    return jsonify({"ok": True, "sent": rooms})


@app.post("/api/maintenance")
def api_maintenance():
    body = request.get_json(force=True) or {}
    key = body.get("part")
    if key not in PARTS_DEFAULT:
        return jsonify({"ok": False, "error": "unknown part"}), 400
    cfg = load_json(MAINTENANCE, {})
    now = counters(bridge.snapshot())
    rec = cfg.get(key, {})
    rec["replaced_on"] = body.get("date") or date.today().isoformat()
    rec["counter_at"] = now.get(PARTS_DEFAULT[key]["counter"])
    cfg[key] = rec
    save_json(MAINTENANCE, cfg)
    bridge.event("maintenance", f"{PARTS_DEFAULT[key]['name']} replaced on {rec['replaced_on']}")
    return jsonify({"ok": True, "maintenance": maintenance_overview(bridge.snapshot())})


@app.get("/api/report")
def api_report():
    """Factual overview from the robot's own counters (for a warranty claim or complaint)."""
    s = bridge.snapshot()
    return jsonify({
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "name": s.get("name"), "sku": s.get("sku"), "firmware": s.get("softwareVer"),
        "nav_serial": dig(s, "hwPartsRev", "navSerialNo"), "blid": bridge.blid,
        "missions": s.get("bbmssn"), "run": s.get("bbrun"), "resets": s.get("bbrstinfo"),
        "charging": s.get("bbchg"), "battery": s.get("batInfo"), "system_hours": s.get("bbsys"),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
