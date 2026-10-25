#!/usr/bin/env python3
"""Exploration script for the Roomba (local, without the iRobot cloud).

Usage (from the project directory, with .venv):
  .venv/bin/python explore.py discover                  # find the robot on the LAN
  .venv/bin/python explore.py password                  # fetch the password locally (pairing mode)
  .venv/bin/python explore.py password --cloud          # fallback: through the iRobot account (dorita980)
  .venv/bin/python explore.py dump  [--seconds 60] [--label idle]
  .venv/bin/python explore.py watch [--label vacuum]    # follow live changes, Ctrl-C stops
  .venv/bin/python explore.py diff dumps/a.json dumps/b.json
  .venv/bin/python explore.py cmd find|dock|stop|pause|resume|start

Credentials live in .env (ROOMBA_IP, ROOMBA_BLID, ROOMBA_PASSWORD) and never in git.
Note: the robot accepts only one local connection at a time. Close the iRobot app while pairing.
"""
import argparse
import getpass
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DUMPS = HERE / "dumps"
ENV = HERE / ".env"
MQTT_PORT = 8883
DISCOVERY_PORT = 5678
MAGIC = bytes([0xF0, 0x05, 0xEF, 0xCC, 0x3B, 0x29, 0x00])


# ---------- helpers ----------
def ts():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("ROOMBA_IP", "ROOMBA_BLID", "ROOMBA_PASSWORD"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def save_env(env):
    lines = [f"{k}={v}" for k, v in env.items()]
    ENV.write_text("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)
    print(f"-> saved in {ENV} (readable by you only)")


def need(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}. Run 'discover' and 'password' first.")


def deep_merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # the robot has its own iRobot CA; we pin on the IP instead
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    except ssl.SSLError:
        pass
    return ctx


# ---------- discover ----------
def discover(args):
    env = load_env()
    found = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.settimeout(1.0)
    s.bind(("", DISCOVERY_PORT))
    targets = ["255.255.255.255"] + ([env["ROOMBA_IP"]] if env.get("ROOMBA_IP") else []) + ([args.ip] if args.ip else [])
    for _ in range(3):
        for t in targets:
            s.sendto(b"irobotmcs", (t, DISCOVERY_PORT))
        time.sleep(0.3)

    def listen(seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, addr = s.recvfrom(8192)
            except socket.timeout:
                continue
            if data == b"irobotmcs":
                continue
            try:
                found[addr[0]] = json.loads(data.decode(errors="replace"))
            except json.JSONDecodeError:
                found[addr[0]] = {"raw": data.decode(errors="replace")}

    listen(args.seconds)
    if not found:
        # Broadcast does not reach the robot on some routers (KPN Box); unicast does. Sweep the /24.
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 53))  # no traffic is sent; only used to learn our own LAN address
        base = probe.getsockname()[0].rsplit(".", 1)[0]
        probe.close()
        print(f"No answer to broadcast, sweeping {base}.0/24 with unicast...")
        for i in range(1, 255):
            try:
                s.sendto(b"irobotmcs", (f"{base}.{i}", DISCOVERY_PORT))
            except OSError:
                pass  # ARP queue full: give it a moment and continue
            time.sleep(0.01)
        listen(3)
    s.close()
    if not found:
        print("No robot found. Is it on and on the same network? Try --ip <address>.")
        return 1
    DUMPS.mkdir(exist_ok=True)
    out = DUMPS / f"discover-{ts()}.json"
    out.write_text(json.dumps(found, indent=2))
    for ip, info in found.items():
        blid = info.get("robotid") or re.sub(r"^(iRobot|Roomba)-", "", info.get("hostname", ""))
        print(f"{ip}  name={info.get('robotname')}  blid={blid}")
        print(f"      firmware={info.get('sw')}  sku={info.get('sku')}  proto={info.get('proto')}  ver={info.get('ver')}")
        cap = info.get("cap") or {}
        print(f"      capabilities: {', '.join(f'{k}={v}' for k, v in sorted(cap.items()))}")
        if len(found) == 1 and blid:
            env["ROOMBA_IP"] = ip
            env["ROOMBA_BLID"] = blid
            save_env(env)
    print(f"-> full answer in {out}")
    return 0


# ---------- password ----------
def password_local(env):
    need(env, "ROOMBA_IP")
    ip = env["ROOMBA_IP"]
    print("Put the robot in pairing mode: robot on the dock, iRobot app fully closed.")
    print("Hold the CLEAN button for about 2 s until it plays a tone sequence and the Wi-Fi light blinks.")
    print("This script then tries to fetch the password every 2 s for 60 s.")
    input("Press Enter once you have pressed the button... ")
    ctx = tls_context()
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            raw = socket.create_connection((ip, MQTT_PORT), timeout=5)
            sock = ctx.wrap_socket(raw)
            sock.sendall(MAGIC)
            sock.settimeout(5)
            buf = b""
            while True:
                try:
                    chunk = sock.recv(1024)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) >= 2 and len(buf) >= 2 + buf[1]:
                    break
            sock.close()
        except OSError as e:
            print(f"  connection failed: {e}")
            time.sleep(2)
            continue
        if len(buf) > 7:
            pw = buf[7:].decode("utf-8", errors="replace").strip("\x00")
            print(f"  PASSWORD RECEIVED ({len(pw)} characters)")
            env["ROOMBA_PASSWORD"] = pw
            save_env(env)
            return 0
        print(f"  robot answers {buf.hex()} = not in pairing mode, retrying...")
        time.sleep(2)
    print("Failed. On newer firmware the button method often no longer works; use 'password --cloud'.")
    return 1


def password_cloud(env):
    print("Fallback through the iRobot cloud with dorita980 (one-off; your account details go to iRobot, not to this script).")
    user = input("iRobot account e-mail: ").strip()
    pw = getpass.getpass("iRobot account password: ")
    cmd = ["npx", "-y", "-p", "dorita980", "get-roomba-password-cloud", user, pw]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = res.stdout + res.stderr
    DUMPS.mkdir(exist_ok=True)
    print(re.sub(re.escape(pw), "***", out))
    blid = env.get("ROOMBA_BLID", "")
    m = re.search(rf'"?{re.escape(blid)}"?\s*:\s*\{{(.*?)\}}', out, re.S) if blid else None
    m2 = re.search(r'"password"\s*:\s*"([^"]+)"', m.group(1) if m else out)
    if m2:
        env["ROOMBA_PASSWORD"] = m2.group(1)
        save_env(env)
        return 0
    print("Could not extract the password from the output automatically; put it in .env by hand as ROOMBA_PASSWORD=...")
    return 1


def password(args):
    env = load_env()
    return password_cloud(env) if args.cloud else password_local(env)


# ---------- mqtt ----------
def mqtt_client(env):
    import paho.mqtt.client as mqtt

    need(env, "ROOMBA_IP", "ROOMBA_BLID", "ROOMBA_PASSWORD")
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=env["ROOMBA_BLID"],
                    protocol=mqtt.MQTTv311, clean_session=False)
    c.username_pw_set(env["ROOMBA_BLID"], env["ROOMBA_PASSWORD"])
    c.tls_set_context(tls_context())
    c.tls_insecure_set(True)
    return c


class Collector:
    def __init__(self, label, live=False):
        self.state = {}
        self.label = label
        self.live = live
        self.n = 0
        self.connected = False
        DUMPS.mkdir(exist_ok=True)
        self.stamp = ts()
        self.raw = open(DUMPS / f"raw-{self.stamp}-{label}.jsonl", "a")

    def on_connect(self, client, userdata, flags, rc, props=None):
        print(f"[connected] rc={rc}")
        self.connected = True
        client.subscribe("#")

    def on_disconnect(self, client, userdata, flags, rc, props=None):
        print(f"[disconnected] rc={rc}  (rc 5 = password refused; 'Connection refused'/dropped = app still open?)")

    def on_message(self, client, userdata, msg):
        self.n += 1
        now = datetime.now().isoformat(timespec="seconds")
        try:
            payload = json.loads(msg.payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {"_raw": msg.payload.hex()}
        self.raw.write(json.dumps({"t": now, "topic": msg.topic, "p": payload}) + "\n")
        self.raw.flush()
        reported = payload.get("state", {}).get("reported") if isinstance(payload, dict) else None
        if reported:
            deep_merge(self.state, reported)
            if self.live:
                print(f"{now}  {json.dumps(reported, ensure_ascii=False)}")
        elif self.live:
            print(f"{now}  [{msg.topic}] {json.dumps(payload, ensure_ascii=False)[:300]}")

    def save_state(self):
        out = DUMPS / f"state-{self.stamp}-{self.label}.json"
        out.write_text(json.dumps(self.state, indent=2, sort_keys=True, ensure_ascii=False))
        return out


INTERESTING = ("mop", "pad", "tank", "scrub", "wet", "rank", "overlap", "pass", "vac", "carpet",
               "bin", "eco", "mode", "clean", "pmap", "sched", "lang", "name", "sku", "sw")


def summary(state):
    def g(*path):
        cur = state
        for p in path:
            if not isinstance(cur, dict) or p not in cur:
                return None
            cur = cur[p]
        return cur

    print("\n=== Summary ===")
    print(f"name={g('name')}  sku={g('sku')}  firmware={g('softwareVer')}")
    print(f"battery={g('batPct')}%  phase={g('cleanMissionStatus', 'phase')}  cycle={g('cleanMissionStatus', 'cycle')}  error={g('cleanMissionStatus', 'error')}")
    print(f"bin present={g('bin', 'present')}  bin full={g('bin', 'full')}")
    for k in ("twoPass", "noAutoPasses", "carpetBoost", "vacHigh", "binPause", "openOnly", "ecoCharge", "noPP"):
        if k in state:
            print(f"  {k} = {state[k]}")
    print("\nTop-level keys:")
    print("  " + ", ".join(sorted(state.keys())))
    print("\nKeys that may relate to mopping/vacuuming/passes:")
    for k in sorted(state.keys()):
        if any(w in k.lower() for w in INTERESTING):
            v = json.dumps(state[k], ensure_ascii=False)
            print(f"  {k} = {v[:200]}")
    lc = state.get("lastCommand")
    if lc:
        print("\nlastCommand (the command the app sent last; gold for rooms and mop settings):")
        print("  " + json.dumps(lc, ensure_ascii=False))


def dump(args):
    env = load_env()
    col = Collector(args.label)
    c = mqtt_client(env)
    c.on_connect, c.on_message, c.on_disconnect = col.on_connect, col.on_message, col.on_disconnect
    print(f"Connecting to {env['ROOMBA_IP']}:{MQTT_PORT} and listening for {args.seconds} s...")
    c.connect(env["ROOMBA_IP"], MQTT_PORT, keepalive=60)
    c.loop_start()
    time.sleep(args.seconds)
    c.loop_stop()
    c.disconnect()
    out = col.save_state()
    print(f"\n{col.n} messages received, state saved in {out}")
    summary(col.state)
    return 0 if col.connected else 1


def watch(args):
    env = load_env()
    col = Collector(args.label, live=True)
    c = mqtt_client(env)
    c.on_connect, c.on_message, c.on_disconnect = col.on_connect, col.on_message, col.on_disconnect
    print(f"Watching live ({args.label}). Now do in the iRobot app what you want to capture. Ctrl-C stops and saves.")
    c.connect(env["ROOMBA_IP"], MQTT_PORT, keepalive=60)
    try:
        c.loop_forever()
    except KeyboardInterrupt:
        pass
    c.disconnect()
    out = col.save_state()
    print(f"\n{col.n} messages, state in {out}")
    summary(col.state)
    return 0


def diff(args):
    a = json.loads(Path(args.a).read_text())
    b = json.loads(Path(args.b).read_text())

    def walk(x, y, path=""):
        keys = sorted(set(x) | set(y)) if isinstance(x, dict) and isinstance(y, dict) else None
        if keys is None:
            if x != y:
                print(f"{path}: {json.dumps(x, ensure_ascii=False)[:120]}  ->  {json.dumps(y, ensure_ascii=False)[:120]}")
            return
        for k in keys:
            p = f"{path}.{k}" if path else k
            if k not in x:
                print(f"{p}: (new) {json.dumps(y[k], ensure_ascii=False)[:120]}")
            elif k not in y:
                print(f"{p}: (gone) was {json.dumps(x[k], ensure_ascii=False)[:120]}")
            else:
                walk(x[k], y[k], p)

    walk(a, b)
    return 0


def cmd(args):
    env = load_env()
    col = Collector(f"cmd-{args.command}")
    c = mqtt_client(env)
    c.on_connect, c.on_message, c.on_disconnect = col.on_connect, col.on_message, col.on_disconnect
    c.connect(env["ROOMBA_IP"], MQTT_PORT, keepalive=60)
    c.loop_start()
    time.sleep(2)
    payload = {"command": args.command, "time": int(time.time()), "initiator": "localApp"}
    print(f"-> cmd {json.dumps(payload)}")
    c.publish("cmd", json.dumps(payload), qos=0)  # qos 0 only: the robot silently ignores qos 1
    time.sleep(args.seconds)
    c.loop_stop()
    c.disconnect()
    out = col.save_state()
    print(f"{col.n} messages after the command, state in {out}")
    summary(col.state)
    return 0


def main():
    sys.stdout.reconfigure(line_buffering=True)  # visible immediately through a pipe/ssh as well
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("discover"); s.add_argument("--ip"); s.add_argument("--seconds", type=int, default=5); s.set_defaults(f=discover)
    s = sub.add_parser("password"); s.add_argument("--cloud", action="store_true"); s.set_defaults(f=password)
    s = sub.add_parser("dump"); s.add_argument("--seconds", type=int, default=60); s.add_argument("--label", default="idle"); s.set_defaults(f=dump)
    s = sub.add_parser("watch"); s.add_argument("--label", default="live"); s.set_defaults(f=watch)
    s = sub.add_parser("diff"); s.add_argument("a"); s.add_argument("b"); s.set_defaults(f=diff)
    s = sub.add_parser("cmd"); s.add_argument("command", choices=["find", "dock", "stop", "pause", "resume", "start"]); s.add_argument("--seconds", type=int, default=15); s.set_defaults(f=cmd)
    args = p.parse_args()
    sys.exit(args.f(args))


if __name__ == "__main__":
    main()
