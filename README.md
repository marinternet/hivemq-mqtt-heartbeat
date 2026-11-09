# Heartbeat Publisher

Tiny cross-platform agent that tells the **Heartbeat Lifecycle subscriber** "I'm alive" every few seconds over HiveMQ Cloud. Install it on any Windows 10/11 laptop/PC or Debian/Ubuntu/Raspberry Pi box; one subscriber watches them all and sends ntfy alerts when one goes silent.

```
[Win10 laptop] ─┐
[Win11 PC]     ─┼─► HiveMQ Cloud ──(+/heartbeat)──► subscriber ──► ntfy (DOWN / RECOVERED)
[Debian mini PC]┘
[RUT routers]  ─┘
```

**What it sends** (every `HEARTBEAT_INTERVAL` seconds, default 5):

- Topic: `<device_id>/heartbeat` (matches the subscriber's `MQTT_TOPIC=+/heartbeat`)
- Payload:
  ```json
  {"heartbeat": {"imei": "4c4c4544004a3510...", "seq": 42, "ts": 1762680000,
                 "hostname": "OPS-LAPTOP-03", "os": "Windows 10 (10.0.22631)",
                 "uptime_s": 86400, "cpu_pct": 7.5, "mem_pct": 61.2}}
  ```
  The subscriber reads the `imei` field, so the device ID goes there even for PCs.

## Device ID

Leave `DEVICE_ID` empty and the publisher picks a stable ID automatically:

| `DEVICE_ID_SOURCE` | Windows | Linux | Notes |
|---|---|---|---|
| `machine-id` (default) | registry `MachineGuid` | `/etc/machine-id` | Stable, no admin needed. Changes only on OS reinstall. |
| `serial` | BIOS serial | DMI serial (root) / Pi board serial | Matches the sticker. Falls back to machine-id when the BIOS says "Default string" etc. |
| `hostname` | computer name | hostname | Readable, but changes if the PC is renamed. |

Or set `DEVICE_ID=anything-you-like` to choose it yourself.

> Machines cloned from the same disk image share a machine-id. For those, set `DEVICE_ID` by hand.

Print the ID on any device with `--show-id`, then add it to the **subscriber's** `.env`:

```
DEVICE_MAPPING={"863409070360333": "Mawar 21 RUT241 Display Aerial", ..., "4c4c4544004a3510...": "Ops Laptop 03"}
```

and restart the subscriber.

---

## One-time: put it on GitHub

```bash
cd heartbeat-publisher
git init && git add . && git commit -m "Heartbeat publisher"
git branch -M main
git remote add origin https://github.com/<you>/heartbeat-publisher.git
git push -u origin main
```

`.env` is in `.gitignore`, so no credentials ever reach GitHub. The repo can be **public** for the easiest cloning, or **private** (then sign in on each device with `gh auth login`, or use a fine-grained token / deploy key).

---

## Install on Windows 10 / 11

In PowerShell (install Git and Python first if missing; open a **new** window afterwards):

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.12

cd C:\
git clone https://github.com/<you>/heartbeat-publisher.git
cd heartbeat-publisher
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
notepad .env                                       # fill in HiveMQ details
venv\Scripts\python.exe publisher.py --once        # test: should say "delivered"
```

Run it in the background at every boot (open **PowerShell as Administrator**):

```powershell
cd C:\heartbeat-publisher
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Service
```

This registers a Task Scheduler task `HeartbeatPublisher` that runs as SYSTEM with no window, starts before anyone logs in, and is relaunched by a 5-minute watchdog if it ever dies. Logs: `C:\heartbeat-publisher\logs\publisher.log`.

Use the python.org/winget Python, not the Microsoft Store one: the SYSTEM account can't run Store apps. Keep the repo in a fixed place like `C:\heartbeat-publisher` (not Downloads/Desktop).

## Install on Debian / Ubuntu / Raspberry Pi OS

```bash
sudo apt update && sudo apt install -y git python3 python3-venv
git clone https://github.com/<you>/heartbeat-publisher.git
cd heartbeat-publisher
chmod +x scripts/setup.sh
./scripts/setup.sh
nano .env                                   # fill in HiveMQ details
venv/bin/python publisher.py --once         # test: should say "delivered"
./scripts/setup.sh --service                # run at boot via systemd
```

Logs: `journalctl -u heartbeat-publisher -f` (also `logs/publisher.log`).

---

## Updating a device

```bash
git pull
./scripts/setup.sh --service                                         # Linux
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Service   # Windows, Admin
```

Both scripts are safe to re-run: they refresh packages and restart the background process. Your `.env` is never touched.

## Removing

```bash
./scripts/setup.sh --remove-service                                          # Linux
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -RemoveService    # Windows, Admin
```

Then delete the folder.

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `HIVEMQ_HOST` / `HIVEMQ_PORT` | – / `8883` | HiveMQ Cloud cluster (same as subscriber) |
| `HIVEMQ_USER` / `HIVEMQ_PASSWORD` | – | Broker credentials |
| `MQTT_TLS` | `true` | HiveMQ Cloud requires TLS |
| `DEVICE_ID` | auto | Fixed ID; overrides auto-detection |
| `DEVICE_ID_SOURCE` | `machine-id` | `machine-id`, `serial` or `hostname` |
| `HEARTBEAT_TOPIC` | `{device_id}/heartbeat` | Must match subscriber's `+/heartbeat` |
| `HEARTBEAT_INTERVAL` | `5` | Seconds. Subscriber alerts after 20s of silence |
| `MQTT_QOS` | `1` | `0` or `1` |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every heartbeat |
| `LOG_TO_FILE` | `true` | Rotating `logs/publisher.log` (5 × 1 MB) |

Tip: in HiveMQ Cloud → Access Management, create a separate publish-only credential for field devices, so a lost laptop's `.env` can't read everyone's traffic.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Broker refused connection: Not authorized` | Wrong `HIVEMQ_USER` / `HIVEMQ_PASSWORD` |
| `Could not connect ... within 20s` | Check `HIVEMQ_HOST`, port 8883 open on the network/firewall |
| Subscriber shows `Unknown Device (...)` | Add the ID from `--show-id` to `DEVICE_MAPPING` and restart the subscriber |
| Two PCs show as one device | Cloned image → set `DEVICE_ID` manually on one of them |
| Laptop alerts DOWN when lid closed | Expected: sleep stops heartbeats. Change power settings if it should stay online |
| Windows: `running scripts is disabled` | Use the `powershell -ExecutionPolicy Bypass -File ...` form shown above |
| Debian: `ensurepip is not available` | `sudo apt install -y python3-venv`, then re-run setup |
