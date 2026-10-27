"""Persistent local MQTT connection to the Roomba. Keeps the merged state, logs events to
data/events.jsonl and sends commands / preferences."""
import json
import os
import ssl
import threading
import time
from collections import deque
from datetime import datetime

import paho.mqtt.client as mqtt

MQTT_PORT = 8883


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def dig(d, *path):
    for p in path:
        if not isinstance(d, dict) or p not in d:
            return None
        d = d[p]
    return d


class Bridge:
    def __init__(self, ip, blid, password, data_dir):
        self.ip, self.blid = ip, blid
        self.data_dir = data_dir
        self.state = {}
        self.lock = threading.Lock()
        self.connected = False
        self.last_error = None
        self.last_msg = None
        self.recent = deque(maxlen=300)
        self.events_path = os.path.join(data_dir, "events.jsonl")
        self._watch_prev = {}
        self.pause_until = 0  # release the connection for a while so the iRobot app can connect locally (command capture)
        # Safety net: evacuate the bin after every vacuum run. The robot normally does this itself
        # (phase evac before cycle none); if that phase never shows up, we send 'evac' once it is on the dock.
        self._cycle_active = None      # None = no state seen yet
        self._evac_seen = False
        self._cycle_mode = None        # operatingMode of the current run (4 = mop only: no evac needed)
        self._last_auto_evac = 0       # at most one auto-evac per 10 min (never a loop)

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=blid,
                        protocol=mqtt.MQTTv311, clean_session=False)
        c.username_pw_set(blid, password)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        c.tls_set_context(ctx)
        c.tls_insecure_set(True)
        c.reconnect_delay_set(2, 30)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c

    # ---- lifecycle ----
    def start(self):
        threading.Thread(target=self._run, daemon=True, name="mqtt").start()

    def _run(self):
        while True:
            while time.time() < self.pause_until:
                self.connected = False
                self.last_error = f"bridge paused until {datetime.fromtimestamp(self.pause_until):%H:%M:%S} (iRobot app allowed)"
                time.sleep(1)
            try:
                self.client.connect(self.ip, MQTT_PORT, keepalive=60)
                self.client.loop_forever(retry_first_connection=False)
            except Exception as e:  # app holds the connection, robot off, network gone
                self.connected = False
                self.last_error = f"{datetime.now():%H:%M:%S} {e}"
            time.sleep(5)

    def pause(self, minutes):
        """Release the connection; afterwards the bridge reconnects by itself and logs a changed lastCommand."""
        self.pause_until = time.time() + minutes * 60
        self.event("bridge", f"released for {minutes} min so the iRobot app can connect locally")
        try:
            self.client.disconnect()
        except Exception:
            pass

    def resume(self):
        self.pause_until = 0

    def _on_connect(self, client, userdata, flags, rc, props=None):
        self.connected = rc == 0 or str(rc) == "Success"
        self.last_error = None if self.connected else str(rc)
        self.event("connection", f"connected ({rc})")
        client.subscribe("#")

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected = False
        self.last_error = f"{datetime.now():%H:%M:%S} connection lost ({rc})"
        self.event("connection", f"disconnected ({rc})")

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        self.last_msg = datetime.now().isoformat(timespec="seconds")
        self.recent.append({"t": self.last_msg, "topic": msg.topic, "p": payload})
        reported = dig(payload, "state", "reported")
        if not isinstance(reported, dict):
            return
        with self.lock:
            deep_merge(self.state, reported)
            self._detect_events()

    # ---- events ----
    WATCH = {
        "phase": ("cleanMissionStatus", "phase"),
        "cycle": ("cleanMissionStatus", "cycle"),
        "error": ("cleanMissionStatus", "error"),
        "notReady": ("cleanMissionStatus", "notReady"),
        "dockError": ("dock", "error"),
        "binFull": ("bin", "full"),
        "binPresent": ("bin", "present"),
        "tankPresent": ("tankPresent",),
        "pad": ("detectedPad",),
        "dockWater": ("dock", "tankLvl"),
        "dockWasteWater": ("dock", "gwTankLvl"),
        "imgUpload": ("imgUpload",),
        "padWashAllowed": ("padWashAllowed",),
        "appCommand": ("lastCommand",),  # every new command from the iRobot app is captured this way
    }

    def _detect_events(self):
        self._auto_evac()
        for name, path in self.WATCH.items():
            v = dig(self.state, *path)
            # Keep a serialized copy: deep_merge mutates dicts in place, so a reference would never "change"
            # (bug 2026-10-24: appCommand was never logged because of that)
            snap = json.dumps(v, sort_keys=True, ensure_ascii=False)
            if name not in self._watch_prev:
                self._watch_prev[name] = snap
                continue
            if snap != self._watch_prev[name]:
                if name == "appCommand":
                    text = snap
                else:
                    text = f"{json.loads(self._watch_prev[name])} -> {v}"
                self.event(name, text, extra={"mission": dig(self.state, "cleanMissionStatus", "missionId")})
                self._watch_prev[name] = snap

    def _mode_from_lastcommand(self):
        lc = self.state.get("lastCommand") or {}
        p = lc.get("params") or ((lc.get("regions") or [{}])[0].get("params")) or {}
        return p.get("operatingMode")

    def _auto_evac(self):
        cms = self.state.get("cleanMissionStatus") or {}
        cycle, phase = cms.get("cycle"), cms.get("phase")
        active = cycle == "clean"  # only real cleaning runs; evac/dock cycles do not count (loop bug 2026-10-27)
        if self._cycle_active is None:  # first state after (re)start of the bridge
            self._cycle_active, self._evac_seen, self._cycle_mode = active, False, self._mode_from_lastcommand()
            return
        if active and not self._cycle_active:            # a new run begins
            # cleanMissionStatus.operatingMode is often still 0 at the start; the command itself is reliable
            self._evac_seen, self._cycle_mode = False, self._mode_from_lastcommand() or cms.get("operatingMode")
        if phase == "evac":                                # after the reset, or an evac in the same message is lost
            self._evac_seen = True
        if self._cycle_active and not active:             # run finished
            bin_ = self.state.get("bin") or {}
            if (phase == "charge" and not self._evac_seen and self._cycle_mode != 4
                    and bin_.get("present") and self.state.get("evacAllowed", True)
                    and time.time() - self._last_auto_evac > 600):
                self._last_auto_evac = time.time()
                self.event("autoEvac", f"run finished without an evac phase (mode {self._cycle_mode}); emptying the bin")
                threading.Thread(target=self.command, args=("evac",), daemon=True).start()
        self._cycle_active = active

    def event(self, type_, text, extra=None):
        rec = {"t": datetime.now().isoformat(timespec="seconds"), "type": type_, "text": text}
        if extra:
            rec.update({k: v for k, v in extra.items() if v is not None})
        try:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def events(self, n=50):
        try:
            with open(self.events_path) as f:
                lines = f.readlines()[-n:]
            return [json.loads(l) for l in lines][::-1]
        except (OSError, json.JSONDecodeError):
            return []

    # ---- sending ----
    def command(self, name, extra=None):
        payload = {"command": name, "time": int(time.time()), "initiator": "localApp"}
        if extra:
            payload.update(extra)
        self.event("command", json.dumps(payload, ensure_ascii=False))
        info = self.client.publish("cmd", json.dumps(payload), qos=0)  # qos 0: the robot silently ignores qos-1 publishes (measured 2026-10-24)
        try:
            info.wait_for_publish(timeout=5)
        except (ValueError, RuntimeError):
            pass
        return info.rc == 0, payload

    def set_prefs(self, prefs):
        payload = {"state": prefs}
        self.event("preference", json.dumps(prefs, ensure_ascii=False))
        info = self.client.publish("delta", json.dumps(payload), qos=0)
        try:
            info.wait_for_publish(timeout=5)
        except (ValueError, RuntimeError):
            pass
        return info.rc == 0, payload

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))
