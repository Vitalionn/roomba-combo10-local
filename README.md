# Roomba Combo 10 Max — local control without the iRobot cloud

Fully local control of an iRobot **Roomba Combo 10 Max** (firmware `topaz+1.12.11`, sku `x085840`, AutoWash dock)
over the robot's own MQTT interface, with the robot's internet access blocked at the router. Start/stop/dock,
whole-house and **per-room** cleaning, vacuum-only / mop-only / vacuum+mop, water level, SmartScrub, suction,
passes, bin evacuation, dock status, privacy switches (image upload off), maintenance counters and a plain web UI.

Everything here was measured on one robot in October 2026. Nothing touches the firmware; the robot keeps using
the same local interface the official app uses on the home Wi-Fi. If you block the internet the official app loses
the robot, but the robot keeps working locally (verified over 20+ hours and several missions, including a mid-mission
return to the dock for bin emptying and water refill).

The existing libraries ([dorita980](https://github.com/koalazak/dorita980),
[Roomba980-Python](https://github.com/NickWaterton/Roomba980-Python),
[ha_roomba_plus](https://github.com/Rickeeh/ha_roomba_plus)) were built around the 980/i7/j7. The Combo 10 Max is
not documented there, and at least one of them lists the newer Combo models as cloud-only. It is not. The
findings below are the things that differ from, or are missing in, those repos.

## Findings that are not in the other repos

1. **Only QoS 0 works.** Publishing to `cmd` or `delta` with QoS 1 gets a PUBACK from the robot's broker, but the
   robot never acts on the message and `lastCommand` does not change. Everything looked "ignored" until we went
   back to QoS 0.
2. **`lastCommand` is a normalized echo, not the wire format.** It always contains every key
   (`favorite_id`, `ordered`, `params`, `pmap_id`, `regions`, `user_pmapv_id`), filled with `null`. If you copy that
   echo back as a per-room start, including `"params": null` and `"favorite_id": null`, the robot silently drops the
   whole command. Leave those keys out.
3. **Two different `params` field sets**, measured from the official app's own commands:
   - whole house: `carpetBoost, noAutoPasses, operatingMode, padWetness, swScrub, twoPass, vacHigh`
   - per room (inside each region): `carpetBoost, gentleMode, noAutoPasses, operatingMode, padWashAfter, twoPass, vacHigh`,
     plus `padWetness` and `swScrub` **only** when `operatingMode` is a mop mode (4 or 6).

   One unknown or extra key, and the start is silently ignored. No error code, nothing in `lastCommand`.
4. **Mop missions do not start in `run`.** They begin on the dock with `evac`, `refill` and/or `padWash` and only
   then move to `run`. Waiting for `phase == "run"` to confirm a start produces false negatives. The reliable
   acceptance check is: your own `time` value shows up in `lastCommand` and `cleanMissionStatus.cycle == "clean"`.
5. **After `stop` the robot is not ready for about 12 s** (`notReady` 39 → 23 → 0). A `start` in that window is
   ignored. Do not "stop first, then start".
6. **`dock` while driving is ignored**; send `stop`, wait for `notReady == 0`, then `dock`.
7. **The button pairing method does not work on firmware 1.12.11** (the robot drops off the network for ~20 s and no
   password is returned). The cloud route works: see `cloud_password.py`, which is the Roomba980-Python cloud password
   flow with a configurable country code (an EU account lives in a different Gigya datacenter; dorita980 hardcodes US
   and fails with "Invalid LoginID").
8. **Discovery by UDP broadcast may not work** (it did not through a KPN Box router); unicast to each address on the
   /24 does. `explore.py discover` sweeps the subnet.
9. **Room IDs and map IDs can be read without the cloud.** Release the local MQTT connection for a few minutes,
   start a run with *some* rooms ticked in the official app on the same Wi-Fi (with all rooms ticked the app sends a
   plain start without a region list), reconnect, and read `pmap_id`, `user_pmapv_id` and `regions` from
   `lastCommand`. Do not stop the mission from the app before reading: `pause`/`stop` overwrite `lastCommand`.
   An old `user_pmapv_id` keeps being accepted after the robot has bumped its own map version.
10. **Privacy switches are accepted locally** via `delta`: `{"state": {"imgUpload": 0}}` is confirmed by the robot.
    Also present: `mapUploadAllowed`, `pmapLearningAllowed`, `padWashAllowed`, `padDryAllowed`, `childLock`.
11. **The robot accepts more than one local connection in practice**: the official app on the same Wi-Fi sent
    commands (initiator `localApp` and `rmtApp`) while our bridge stayed connected. But if the app is connected first,
    a second client often gets no connection at all. Close the app while pairing.
12. **The robot's clock drifts without NTP** (about +19 s after a day). It stores the `time` you send in
    `lastCommand` unchanged, so clock drift did not cause rejections.

## Protocol summary (as measured)

- Discovery: UDP 5678, payload `irobotmcs`; the reply includes `hostname` (`Roomba-<BLID>`), `sw`, `sku`, `cap`.
- MQTT over TLS on port 8883, client id = BLID, username = BLID, password = the robot password, MQTT 3.1.1,
  `clean_session` false, certificate verification off (self-signed). Subscribe to `#`; the robot publishes its state
  as `{"state": {"reported": {...}}}` in chunks, plus `wifistat` and `$aws/things/<BLID>/shadow/update`.
- Commands: topic `cmd`, QoS 0, `{"command": "<name>", "time": <unix seconds>, "initiator": "localApp", ...}`.
  Seen working: `start`, `stop`, `pause`, `resume`, `dock`, `find`, `evac`, `train`.
- Preferences: topic `delta`, QoS 0, `{"state": {"<field>": <value>}}`.
- Start parameters:

  | Setting | Field(s) | Values |
  |---|---|---|
  | vacuum only / mop only / vacuum + mop | `operatingMode` | 2 / 4 / 6 (all measured) |
  | suction eco / normal / high | `carpetBoost`, `vacHigh` | normal = true/false (measured); eco = false/false, high = false/true (dorita980, not yet verified here) |
  | water eco / standard / max | `padWetness` | `{"disposable": n, "reusable": n}`, n = 1 / 2 / 3 |
  | SmartScrub | `swScrub` | 0 / 1 |
  | passes 1 / 2 / auto | `noAutoPasses`, `twoPass` | 1 = true/false (measured); 2 = false/true, auto = false/false (dorita980, not yet verified here) |
  | per-room only | `gentleMode`, `padWashAfter` | 0 / 1 |

- Per-room start (working example):

  ```json
  {"command": "start", "time": 1792868753, "initiator": "localApp", "ordered": 1,
   "pmap_id": "<pmap_id>", "user_pmapv_id": "<user_pmapv_id>",
   "regions": [{"region_id": "4", "type": "rid",
                "params": {"carpetBoost": true, "gentleMode": 0, "noAutoPasses": true, "operatingMode": 2,
                           "padWashAfter": 0, "twoPass": false, "vacHigh": false}}]}
  ```

- Mission phases seen: `charge, run, stop, hmUsrDock, hmMidMsn, hmPostMsn, evac, refill, padWash, stuck`.
  Cycles: `none, clean, dock, evac`. `notReady` values seen: 2, 4, 23, 39, 68 (39 = pending after stop, 23/68 =
  internal readiness check, both undocumented; 2 = uneven ground per community).
- Dock fields: `dock.tankLvl` (clean water %, reads 0 while the robot is away), `dock.gwTankLvl` (waste water %),
  `dock.error`, state codes `state/pwState/pdState/frState` (300/600/700/400 idle; 701 → 702 after a mop run looked
  like pad drying; meaning otherwise not decoded). Robot: `tankLvl`, `tankPresent`, `detectedPad`
  (`reusableWet` etc.), `bin.full`, `bin.present`.
- Capabilities from discovery on this unit: `scrub=1, multiPass=2, carpetBoost=1, eco=1, pmaps=9, sched=2, matter=1`.
  Matter is a second, standardized local route (not explored).

## What is in this repo

- `app/` — Flask + paho-mqtt bridge and a plain web UI. Keeps the merged state, logs events to
  `data/events.jsonl`, exposes `/api/state`, `/api/start`, `/api/cmd`, `/api/prefs`, `/api/raw`, `/api/recent`,
  `/api/report` (counters for a warranty claim) and a "let the official app connect for N minutes" switch for
  capturing commands. Auto-evac safety net after vacuum runs.
- `explore.py` — exploration CLI: `discover`, `password`, `dump`, `watch`, `diff`, `cmd`.
- `cloud_password.py` — one-time password retrieval through your iRobot account (country-aware).
- `sequence.py` — run cleaning jobs back to back (waits for dock + ready between jobs).
- `USAGE.md` — operating manual: procedures, hard lessons, running jobs back to back.

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install paho-mqtt requests
.venv/bin/python explore.py discover        # writes ROOMBA_IP and ROOMBA_BLID to .env
.venv/bin/python cloud_password.py --country NL   # writes ROOMBA_PASSWORD to .env (close the official app first)
# add BIND_IP=<your LAN/VPN ip> to .env (see .env.example)
docker compose up -d --build               # web UI on http://<BIND_IP>:8083
```

Read the room IDs as described in finding 9, then block the robot's internet access at the router. The robot sends
its counters and logs for the offline period the moment it reconnects, so if privacy is the point, keep it blocked.
Reversible at any time by lifting the block.

There is no authentication in the web app. Bind it to a LAN or VPN address only.

## Not done / open

- Suction high/eco and 2-pass values are taken from dorita980 and not yet verified on this robot.
- Contextual dock commands (refill tank, wash pad, stop drying, flush) are not captured yet; `evac` works.
- Schedules (`cleanSchedule2`) can be set locally but there is no UI for it.
- Map editing stays cloud-only by design.

## Credits

Protocol groundwork by [koalazak/dorita980](https://github.com/koalazak/dorita980) and
[NickWaterton/Roomba980-Python](https://github.com/NickWaterton/Roomba980-Python);
[samm-git/irobot-explore](https://github.com/samm-git/irobot-explore) for the `settings`/`cmd` write notes.
MIT licensed.
