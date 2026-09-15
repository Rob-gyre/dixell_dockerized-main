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

```bash
cd ~/dixell_dockerized-main

# 1. Install Docker if it isn't there (install.sh does this + adds you to the docker group)
./install.sh ~/dixell_dockerized-main
# log out and back in so the docker group applies, or use sudo for now

# 2. Make sure config.yaml and schedule.json exist and are correct for THIS Pi
#    (dev_eui, mqtt client_id, sheet name, certs). In fleet mode Ansible renders
#    these; for a standalone Pi you edit them by hand.
ls -la config.yaml schedule.json

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

A healthy collector cycle looks like:

```
The collector connects to the Dixell controller, checks the scheduled
setpoint and writes it if needed, then applies changed parameters.
It reads the controller, uses the existing cached-value fallback for
missing readings, and queues the payload in SQLite.

### Fallback setpoint

The collector normally uses the current hour’s setpoint from SharePoint or
`schedule.json`. If neither contains one, it uses `fallback_setpoint` from
`config.yaml`.

Set this value for the room type at each installation. For example, a chill
room may use `3.0`, while a freezer may use `-18.0`. The fallback can write to
the controller, so configure it before starting the collector.

The emitter publishes queued readings to MQTT separately.
```

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
