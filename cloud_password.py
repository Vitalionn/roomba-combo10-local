#!/usr/bin/env python3
"""One-off: fetch the robot's local password through your iRobot account, using the right region.

dorita980 always queries the login server with country_code=US; an EU account lives in a different
Gigya datacenter and then gets "Invalid LoginID". This script does the same as
Roomba980-Python/getcloudpassword.py, but with a configurable country (default NL).

Usage:  .venv/bin/python cloud_password.py [--country NL] [--email address]
Writes ROOMBA_PASSWORD to .env if the BLID from .env is in your account. Account details
only go to iRobot/Gigya and are not stored anywhere.
"""
import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"
DISCOVERY = "https://disc-prod.iot.irobotapi.com/v1/discover/endpoints?country_code={cc}"
APP_ID = "ANDROID-C7FB240E-DF34-42D7-AE4E-A8C17079A294"


def load_env():
    env = {}
    if ENV.exists():
        for line in ENV.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def save_env(env):
    ENV.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    os.chmod(ENV, 0o600)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--country", default="NL")
    p.add_argument("--email")
    p.add_argument("--show-all", action="store_true", help="print the full robots JSON (contains passwords)")
    a = p.parse_args()

    disc = requests.get(DISCOVERY.format(cc=a.country), timeout=20)
    disc.raise_for_status()
    d = disc.json()
    gigya = d["gigya"]
    dep = next(iter(d["deployments"].values()))
    print(f"region {a.country}: gigya datacenter={gigya['datacenter_domain']}  api={dep['httpBase']}")

    email = a.email or input("iRobot account e-mail: ").strip()
    pw = getpass.getpass("iRobot account password: ")

    r = requests.post(f"https://accounts.{gigya['datacenter_domain']}/accounts.login", data={
        "apiKey": gigya["api_key"], "targetenv": "mobile", "targetEnv": "mobile",
        "loginID": email, "password": pw, "format": "json",
    }, timeout=20)
    g = r.json()
    if g.get("errorCode", 0) != 0:
        print(f"Gigya login failed: {g.get('errorCode')} {g.get('errorDetails')}")
        print("Possible causes: different e-mail address, or an account created through Google/Apple sign-in (password login does not work then).")
        return 1

    r = requests.post(f"{dep['httpBase']}/v2/login", json={
        "app_id": APP_ID, "assume_robot_ownership": "0",
        "gigya": {"signature": g["UIDSignature"], "timestamp": g["signatureTimestamp"], "uid": g["UID"]},
    }, timeout=20)
    if r.status_code != 200:
        print(f"iRobot login failed: HTTP {r.status_code} {r.text[:300]}")
        return 1
    robots = r.json().get("robots", {})
    if not robots:
        print("Login succeeded but there are no robots in this account.")
        return 1

    env = load_env()
    blid = env.get("ROOMBA_BLID", "")
    for rid, info in robots.items():
        mark = " <- this one is in .env" if rid == blid else ""
        print(f"robot {rid}: name={info.get('name')} sku={info.get('sku')} sw={info.get('softwareVer')}{mark}")
    if a.show_all:
        print(json.dumps(robots, indent=1))
    if blid in robots and robots[blid].get("password"):
        env["ROOMBA_PASSWORD"] = robots[blid]["password"]
        save_env(env)
        print(f"-> ROOMBA_PASSWORD saved in {ENV}")
        return 0
    print("BLID from .env not found in the account, or no password field. Run with --show-all to inspect.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
