# Dixell XR77U — Docker Stack: Operations & Deployment

The Dixell collector + emitter, containerized, running on a Raspberry Pi
(`/home/gyre/dixell_dockerized-main`). This doc covers **manual operation on the Pi
first** — the day-to-day you run by hand over SSH — and then, at the end, how
each of those actions is triggered from the Ansible/gateway side once fleet
management is in play.

Read the manual sections to understand what actually happens on the box. The
Ansible layer just automates these same commands across many Pis.

---

## 0. What's in the folder

```
/home/gyre/dixell_dockerized-main/
├── Dockerfile                    image definition (python + pyserial + deps)
├── docker-compose.yml            the 3 services: collector, emitter, verify
├── supervisor.py                 runs a script on a fixed interval, handles SIGTERM
├── wait_and_run.py               waits for /dev/ttyUSB0, then hands off to supervisor
├── collector_shaprepoint.py      reads schedule, writes setpoint and parameters, polls, queues to DB
├── emitter.py                    drains unsent DB rows to MQTT over TLS
├── view_queue.py                 DB dashboard (collected/emitted per day)
├── config.yaml                   ALL config (serial, MQTT, SharePoint, certs)  ← rendered by Ansible in fleet mode
├── schedule.json                 baseline schedule + params                    ← rendered by Ansible in fleet mode
├── requirements.txt              Python dependencies installed during image build
├── dixell_modbus.py              Dixell Modbus RTU communication
├── xr77u.json                    XR77U register addresses, scaling and parameter limits
├── readings.db                   SQLite queue (runtime; never in git/image)
├── last_readings.json            cache of last good poll (runtime)
├── sync_state.json               SharePoint failure counter (runtime)
├── last_setpoint.json            last written setpoint (runtime)
└── *.lock                        flock guards (runtime)
```

Two long-running containers:

| Container | Command | Interval | Touches serial? | Touches MQTT? |
|-----------|---------|----------|-----------------|---------------|
| `dixell-collector` | `wait_and_run.py /dev/ttyUSB0 collector_shaprepoint.py 900` | 15 min | yes | no |
| `dixell-emitter` | `supervisor.py emitter.py 300` | 5 min | no | yes |

Both have `restart: unless-stopped`, so they survive reboots (the collector
waits for `/dev/ttyUSB0` if the Pi boots with the controller unplugged).

---

## 1. First-time setup on a Pi

Run these on the Pi itself (SSH in as `gyre`).
`config.example.yaml` is the credential-free installation template.
Keep credentials only in the Pi's local `config.yaml`. MQTT certificate
fields (`client_cert`, `client_key`, `ca_cert`) contain base64-encoded
certificate/key contents, not file paths. Teams uses `teams.webhook`.

Set `_meta.dev_eui` and `_meta.device_name` in `schedule.json`.
SharePoint supplies the worksheet's schedule, parameters and identity;
these two metadata fields remain sourced from the local baseline file.

Starting the collector can write setpoints and parameters to the controller.
Configure the installation before running the start commands below.
If MQTT is not configured, start only the collector:

`docker compose up -d collector`

```bash
cd ~/dixell_dockerized-main

# 1. Install Docker if it isn't there (install.sh does this + adds you to the docker group)
./install.sh ~/dixell_dockerized-main
# log out and back in so the docker group applies, or use sudo for now

# 2. Create the local configuration if it does not already exist.
# config.yaml is excluded from Git; git pull does not supply or update it.
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
fi

# Configure serial settings, room fallbacks and SharePoint.
# Configure MQTT and Teams only when those services are required.
nano config.yaml

# Set the device identity, baseline hourly schedule and parameters.
nano schedule.json

# Check the schedule syntax before starting collection.
python3 -m json.tool schedule.json

# 3. Confirm the controller is on the bus
ls -la /dev/ttyUSB0

# 4. Build the image
docker compose build

# 5. Start the stack
docker compose up -d collector emitter

# 6. Watch it run
docker compose logs -f collector
docker compose logs -f emitter
```

### Collector operation

The collector obtains the schedule, checks the target setpoint and applies
parameter changes. It then reads the Dixell controller and queues the payload
in SQLite. Missing readings use the existing cached-value fallback.

The emitter publishes queued readings to MQTT separately.

### Schedule selection and fallback values

The collector first attempts to fetch the configured SharePoint worksheet.
A successful fetch is saved to `live_config.json`.

If fetching fails, the collector can use that cached configuration. Once
`offline_failure_threshold` is reached, it prefers the local `schedule.json`.
If no cached configuration exists, it also tries `schedule.json`.

Within the selected schedule, the setpoint priority is:

1. The current hour's setpoint.
2. `parameters.setpoint`.
3. `fallback_setpoint` from `config.yaml`.

The differential uses `parameters.dif_c` when supplied. Otherwise, it uses
`fallback_dif_c` from `config.yaml`.

Configure both fallback values for each room before starting the collector:

```yaml
fallback_setpoint: 2.0
fallback_dif_c: 2.0
```

These are example values, not recommendations for every room type.
Fallback values can be written to the controller.

If the configuration keys are omitted, the code still defaults to a
setpoint of 5.0°C and a differential of 2.0°C. Specify both keys explicitly
for each installation.

The fallback values do not create a schedule if all schedule sources are
unavailable.


A healthy emitter cycle:

```
emitter - Found N unsent readings
emitter - Sent id=X devEUI=... (total=1)
emitter - Emission complete. Sent N readings.
supervisor - Sleeping 300s until next run
```

---

## 2. Everyday lifecycle (manual, on the Pi)

All from `~/dixell_dockerized-main`.

```bash
# status
docker compose ps

# stop / start (containers stay defined; state on disk is kept)
docker compose stop
docker compose start

# restart one service (kicks it out of its sleep, picks up config edits)
docker compose restart collector
docker compose restart emitter

# stop + remove containers (image stays; data on disk stays)
docker compose down

# bring everything back up
docker compose up -d collector emitter

# resource usage snapshot
docker stats --no-stream dixell-collector dixell-emitter
```

**Power cycle:** with `restart: unless-stopped`, both come back automatically
after a reboot or power loss — *unless* you explicitly ran `docker compose stop`
/ `down`, which is remembered until the next `up`.

---

## 3. "Something changed" — what to run

### 3a. You edited `config.yaml` (MQTT topic, cert, SharePoint sheet, interval note)

Config is read at the start of every cycle, but a running container won't
re-read mid-sleep. Force it:

```bash
docker compose restart collector emitter
docker compose logs --tail 20 collector
```

No rebuild needed — `config.yaml` is bind-mounted, not baked into the image.

### 3b. You edited `schedule.json` (baseline setpoint / params)

Same as config — it's read each cycle. Apply immediately with a restart:

```bash
docker compose restart collector
docker compose logs -f collector      # confirm the new target setpoint
```

### 3c. You edited the Python (`collector_shaprepoint.py`, `emitter.py`, `supervisor.py`, `wait_and_run.py`)

The code is bind-mounted too, and `supervisor.py` launches a **fresh** `python`
subprocess every cycle — so a code edit is picked up on the next cycle with no
rebuild. To apply now instead of waiting:

```bash
docker compose restart collector      # or emitter
docker compose logs -f collector
```

**Changes to Dockerfile or requirements.txt require an image rebuild.**
See 3d. Changes to dixell_modbus.py or xr77u.json are picked up on the
next collector cycle because the project folder is bind-mounted.

### 3d. You changed the `Dockerfile` or dependencies → rebuild the image

```bash
docker compose build                       # rebuild from Dockerfile
docker compose up -d collector emitter     # recreate containers on the new image
# force a clean rebuild ignoring cache:
docker compose build --no-cache
docker compose up -d --force-recreate collector emitter
```

Editing `collector_shaprepoint.py`/`emitter.py` does **not** need this — only
Dockerfile/dependency changes do.

### 3e. You updated the emitter or collector logic and want it live cleanly

Standard flow:

```bash
# (edit the .py file on the Pi, or in fleet mode Ansible rsyncs it)
docker compose restart collector emitter   # picks up bind-mounted code next cycle
docker compose logs -f collector
```

If you also changed the compose file (e.g. new volume, new interval in the
`command:`), you must recreate, not just restart:

```bash
docker compose up -d --force-recreate collector emitter
```

Rule of thumb:
- **.py or config.yaml or schedule.json changed** → `restart`
- **docker-compose.yml changed** → `up -d --force-recreate`
- **Dockerfile / deps changed** → `build` then `up -d`

---

## 4. Checking logs

```bash
# live tail
docker compose logs -f collector
docker compose logs -f emitter
docker compose logs -f                    # both interleaved

# last N lines
docker compose logs --tail 50 collector

# since a time / duration
docker compose logs --since 1h emitter
docker compose logs --since 2026-06-30T08:00 collector
```

Logs auto-rotate (json-file driver, 10 MB × 5 files per service) — no manual
cleanup needed. The underlying file, if you ever need it:

```bash
docker inspect --format='{{.LogPath}}' dixell-collector
```

---

## 5. Checking the database (what was collected/emitted)

```bash
# full dashboard: totals + per-day collected vs emitted + pending
docker compose run --rm verify

# or run view_queue.py directly with flags
docker compose run --rm --entrypoint python verify view_queue.py --today
docker compose run --rm --entrypoint python verify view_queue.py --days 30
docker compose run --rm --entrypoint python verify view_queue.py --unsent
docker compose run --rm --entrypoint python verify view_queue.py --tail 5
```

Quick raw check without a container:

```bash
sqlite3 readings.db "select count(*) total, sum(sent) sent, sum(1-sent) unsent from queue;"
```

---

## 6. Running a single cycle by hand (debug)

Test one collector cycle without disturbing the scheduled container. **The Dixell
can't take two serial masters at once**, so stop the live collector first:

```bash
docker compose stop collector
docker compose run --rm --entrypoint python collector \
    wait_and_run.py /dev/ttyUSB0 collector_shaprepoint.py 999999
# Ctrl+C after you see "Queued id=... status=..."
docker compose start collector
```

One emitter cycle (safe to run alongside the live emitter — it only touches
SQLite/MQTT, and the lock file prevents a true double-run):

```bash
docker compose run --rm --entrypoint python emitter emitter.py
```

Interactive shell inside a throwaway container:

```bash
docker compose run --rm --entrypoint /bin/bash collector
# ls -la /dev/ttyUSB0 ; python -c "import serial; print(serial.__version__)" ; exit
```

---

## 7. Common issues (manual triage)

- **Dixell write or read-back failed** — check the USB-to-RS485 adapter,
  wiring, controller address and serial settings in `config.yaml`.
  Check that no other program is using the same serial port.
  The collector logs communication failures and uses its existing
  failed-write alert handling.
- **`MQTT connect failed rc=None`** in the emitter — TLS connect timed out.
  Check the Pi clock (`timedatectl`), DNS/reachability to the broker, and the
  cert/key in `config.yaml`. The failed row isn't lost; it retries next cycle.
- **Collector stuck "waiting for /dev/ttyUSB0"** — controller unplugged or
  enumerated elsewhere. `ls -la /dev/ttyUSB*`. It self-heals the moment the
  device appears.
- **Files owned by root** (`readings.db`, `*.json`) — the container writes as
  root. Harmless to the running system; to edit from your shell:
  `sudo chown -R gyre:gyre ~/dixell_dockerized-main`.
- **Duplicate MQTT client_id across Pis** — the emitter on one Pi kicks another
  off the broker (same `client_id`). Each Pi needs a unique `mqtt.client_id`
  (and matching cert). This only bites once >1 Pi runs the emitter.

---

## 8. How this maps to Ansible (fleet mode)

Everything above is what happens on **one** Pi by hand. In fleet mode you don't
SSH into each Pi — you run one command on the gateway and Ansible performs these
same steps over Tailscale. The mapping:

| Manual action on the Pi | Ansible equivalent (run on gateway as `gyreops`) |
|-------------------------|--------------------------------------------------|
| edit `config.yaml` / `schedule.json` | edit `host_vars/bwNN.yml` or `group_vars/all.yml`, then deploy — Ansible **renders** these files from templates |
| `git pull` new `.py` code onto the Pi | `git pull` in the gateway's app clone, then deploy — Ansible **rsyncs** the code |
| `docker compose build` | Ansible runs it in the `controller_stack` role |
| `docker compose up -d collector emitter` | Ansible runs it, then health-checks `docker compose ps` |
| `docker compose restart` (apply a change) | Ansible's `restart stack` handler fires automatically when code/config changed |
| `docker compose logs` / `verify` | `ansible bwNN -a 'cd dixell_dockerized-main && docker compose logs --tail 50 collector'` or `... docker compose run --rm verify` |

The single deploy command (from `/opt/gyre-fleet` on the gateway):

```bash
python3 tools/preflight.py                                  # dup client_id/dev_eui guard
ansible-playbook playbooks/deploy.yml --limit bw35 --check --diff   # preview config diff
ansible-playbook playbooks/deploy.yml --limit bw35          # canary one Pi
ansible-playbook playbooks/deploy.yml --limit site_bw       # all Pis, one at a time
```

What that runs, per Pi, is exactly Sections 1–3 above, in order:
rsync code → render `config.yaml` + `schedule.json` → `docker compose build` →
`up -d collector emitter` → poll `docker compose ps` until both are `running`
(fails the rollout if not). `readings.db` is excluded from the rsync, so a
deploy never wipes a Pi's queue.

**So the mental split is:**
- *Config change* (setpoint, cert, sheet, topic, dev_eui) → edit the Ansible
  repo → deploy. Maps to Section 3a/3b (restart).
- *App change* (new register, log file, logic) → edit the Dixell app repo, pull
  on the gateway → deploy. Maps to Section 3c/3d/3e (rebuild + recreate).

The Ansible role picks the right one: it always re-renders config, rsyncs code,
runs `build` (cheap no-op if nothing changed), and its restart handler only
fires when something actually changed — so you don't have to decide restart-vs-
recreate-vs-rebuild per Pi. That decision (Section 3's rule of thumb) is encoded
in the role.

## Dixell compatibility and testing status

This stack preserves the original Carel collector → SQLite queue → MQTT
emitter architecture. The supervisor, serial-device waiting and queue viewer
are unchanged. Controller communication uses the Dixell Modbus driver and
`xr77u.json` profile.

Existing temperature payload names are retained:

- `probe_temperature`: room probe (Pb1).
- `evap_temperature`: first evaporator probe (Pb2).
- `ai3_temp`: second evaporator probe (Pb3).

Pb2 and Pb3 readings are accepted only when their respective presence
settings, P2P and P3P, confirm they are enabled. Rejected or missing readings
use the existing cached-value fallback when a cached value exists.

### Unresolved Carel field mappings

The following original Carel fields do not yet have confirmed Dixell
equivalents in the payload:

- `comp_min_between`
- `comp_min_off`
- `comp_min_on`
- `def_priority`
- `probe_fault`

Do not assume these fields are available. The general Dixell alarm flag
does not identify a probe fault specifically.

### Profile validation limits

The driver checks writes against the limits and choices in `xr77u.json`.

A validation error can stop a collection cycle after earlier parameters
have already been written. Earlier writes are not rolled back.

### Tests completed on the XR77U test rig

- Docker build and serial communication.
- SharePoint workbook download and worksheet parsing.
- Selected setpoint and parameter writes, with read-back checks.
- SQLite queueing.

MQTT delivery, Teams alerts, connected third-probe operation and remaining
profile mappings still require testing.
