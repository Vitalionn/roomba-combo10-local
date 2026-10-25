# Operating manual

The README covers the findings and the protocol. This file is the day-to-day operation of the app.

## The app

- Container `roomba` (Flask + MQTT bridge): `docker compose up -d --build` in this directory.
- Reachable on `http://<BIND_IP>:8083`. Never expose it publicly: there is no authentication.
- Functions: status and alerts (bin, tanks, dock, errors), start with mode / suction / water / SmartScrub / passes,
  per room, pause / resume / stop / dock / empty bin / beep, switches inside the robot (photo upload, map upload,
  pad washing and drying, child lock), maintenance with replacement dates, counters, event log,
  `/api/report` (factual counters for a warranty claim), `/api/raw` (full state), `/api/recent` (raw MQTT messages).
- Data in `data/`: `events.jsonl` (log), `maintenance.json`, `rooms.json`. Not in git; back it up periodically.
- The bridge holds the one local MQTT connection. The official app on the same Wi-Fi then goes through the cloud
  (as long as that is open) and `explore.py` cannot connect: run `docker compose stop` first.

### Letting the official app connect locally (capturing commands, no cloud needed)

The iRobot app on the home Wi-Fi talks to the robot directly as soon as our bridge releases the single local
connection (measured: `evac` arrived with initiator `localApp` while the cloud was blocked). Button in the app:
"allow iRobot app for 10 min". Afterwards the bridge reconnects by itself and logs a changed `lastCommand` as event
`appCommand`. This is how you capture context buttons of the dock (refill tank, wash pad, stop drying, flush) at the
moment the official app shows them. `evac` = empty the bin.

### Importing rooms

The cloud map API refuses our login (403), so the room ids come from the robot itself: in the iRobot app start a run
with **some** of the rooms ticked (with "all rooms" the app sends no room list, just a plain start), and click
"import rooms from last app command" in our app. That reads `lastCommand` (pmap_id, version, rooms) and stores it in
`data/rooms.json`; lists are merged, so two runs (all-but-one and that one) cover the whole house. Repeat after every
map edit in the iRobot app. Do not stop the run from the iRobot app before importing: `pause`/`stop` overwrite
`lastCommand`. Stop it from our app afterwards.

### Blocking the cloud

1. Finish the map and rooms in the iRobot app, then import the rooms as above.
2. In our app: photo upload off, map upload and map learning off.
3. Router: give the robot a fixed IP (DHCP reservation) and block internet access for that device.
4. Check: `docker logs roomba` keeps saying connected; in the iRobot app the robot shows as offline.

### Reconnecting (always possible)

Lift the block in the router. The robot reconnects to the cloud by itself within minutes and the iRobot app sees it
again. Note: the robot then also uploads its counters and logs for the offline period. Emergency brake: factory reset
on the robot itself; afterwards run `cloud_password.py` again for the new password.

## Exploration (`explore.py`)

Everything from the project directory with the venv, and with the container stopped:

```
.venv/bin/python explore.py discover            # find the robot; writes IP + BLID to .env
.venv/bin/python cloud_password.py             # robot password through the iRobot account (button method fails on 1.12.11)
.venv/bin/python explore.py dump --label idle   # listen 60 s, state -> dumps/state-*.json
.venv/bin/python explore.py watch --label X     # watch live while you do something in the app
.venv/bin/python explore.py diff dumps/a.json dumps/b.json
.venv/bin/python explore.py cmd find            # robot beeps
```

## Hard lessons (measured)

- **Unknown key in `params` = start silently ignored.** A plain start knows exactly the seven whole-house fields.
- **Per-room start needs the per-room field set** and no top-level `params`/`favorite_id` (see README finding 2 and 3).
- **`dock` while driving is ignored**; `stop` first, then `dock`. The app does that automatically.
- **`pause`/`stop`/`resume` overwrite `lastCommand`**: a room list can only be read while the run is still going.
- A paused run that you "start" in the iRobot app becomes a `resume`, not a new start.
- Returning to the dock can take long or fail (10+ min searching) without an error code.
- **Erratic start**: the robot sometimes refuses a start briefly, `notReady` blips to 23 during an internal readiness
  check (`precheck.readiness`). `/api/start` retries up to 3 times; success = own command back in `lastCommand`
  (same `time`) and `cycle=clean`. Never stop first: after a `stop` the robot sits on notReady 39 → 23 → 0 for ~12 s
  and a start in that window is ignored.
- **MQTT always QoS 0.** QoS 1 publishes get a PUBACK but are silently ignored (`cmd` and `delta`).
- Mop runs start on the dock with `evac`/`refill`/`padWash` and only reach `run` later.
- Phases seen in `cleanMissionStatus.phase`: charge, run, stop, hmUsrDock, hmMidMsn, hmPostMsn, evac, refill, padWash, stuck.
- Dock: `tankLvl` = clean water %, `gwTankLvl` = waste water %, `error`, and status codes `state`/`pwState`/`pdState`/`frState`
  (meaning still to be decoded; 701 → 702 seen after a mop run = pad drying).
- The robot's clock runs ahead without NTP (about 19 s after a day); it stores our `time` in `lastCommand` unchanged.

## Running jobs back to back

`sequence.py` starts the next job only when the previous one is finished (robot on the dock, bin emptied,
`notReady` 0 for 30 s). Edit `STEPS` at the bottom, then:

```
ROOMBA_APP=http://<BIND_IP>:8083 setsid nohup python3 sequence.py >/dev/null 2>&1 </dev/null &
tail -f data/sequence.log
```
