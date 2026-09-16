
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dixell PJEZ Collector - Fetches schedule, writes setpoint + params, reads all, queues to SQLite.
 
The EMITTER (emitter.py) handles MQTT publishing from the queue.
This script ONLY collects and queues.
 
Reads and writes use Modbus RTU with the xr77u.json register map.
If readings are missing, we use last cached values as failsafe..
 
Controller-related failures (serial connect, write/read-back, empty poll,
setpoint mismatch) raise a Teams alert via the configured webhook.
 
Run as cron every 5 minutes:
  */5 * * * * /path/to/venv/bin/python /path/to/collector.py
"""
 
import os
import sys
import json
import time
import socket
import logging
import fcntl
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional
from dixell_modbus import DixellXR77U
 
import yaml
 
try:
    import requests
    import pandas as pd
    from io import BytesIO
    SHAREPOINT_OK = True
except ImportError:
    requests = pd = None
    SHAREPOINT_OK = False
 
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Boolean, DateTime, Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker
 
# =========================
# Paths & Config
# =========================
SCRIPT_DIR = Path(__file__).parent.resolve()
CACHE_FILE = SCRIPT_DIR / "last_readings.json"
LIVE_CONFIG_FILE = SCRIPT_DIR / "live_config.json"
SYNC_STATE_FILE = SCRIPT_DIR / "sync_state.json"
LAST_SP_FILE = SCRIPT_DIR / "last_setpoint.json"
 
TZ = ZoneInfo("Europe/London")  # Local timezone for schedule hours (cron runs in UTC)
 
 
def load_config() -> Dict[str, Any]:
    config_path = SCRIPT_DIR / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError("config.yaml not found")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)
 
 
def load_schedule() -> Dict[str, Any]:
    schedule_path = SCRIPT_DIR / "schedule.json"
    if not schedule_path.exists():
        raise FileNotFoundError("schedule.json not found")
    with open(schedule_path, "r", encoding="utf-8") as f:
        return json.load(f)
 
 
def load_json_safe(path) -> Optional[Dict]:
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None
 
 
def save_json_safe(path, data):
    try:
        with open(str(path), "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception:
        pass
 
 
CONFIG = load_config()
 
# =========================
# Logging
# =========================
log_level = CONFIG.get("logging", {}).get("level", "INFO").upper()
LOG = logging.getLogger("collector")
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
 
# =========================
# Teams alerting (controller-related failures)
# =========================
# Identity for Dixell lives in schedule.json (_meta / identity), not config.yaml.
# Loaded eagerly here so alerts identify the Pi even if a failure happens BEFORE
# get_schedule() runs (e.g. serial connect). Refreshed inside collect() once the
# live/SharePoint schedule is resolved.
def _load_alert_identity() -> Dict[str, Any]:
    ident = {"dev_eui": "unknown", "device_name": "unknown",
             "site": "unknown", "company": "unknown", "zone": "", "compressor": ""}
    try:
        sch = load_schedule()
        meta = sch.get("_meta", {})
        idy = sch.get("identity", {})
        ident["dev_eui"] = meta.get("dev_eui", ident["dev_eui"])
        ident["device_name"] = meta.get("device_name", ident["device_name"])
        ident["site"] = idy.get("site", ident["site"])
        ident["company"] = idy.get("company", ident["company"])
        ident["zone"] = idy.get("zone", ident["zone"])
        ident["compressor"] = idy.get("compressor", ident["compressor"])
    except Exception:
        pass
    return ident
 
 
_ALERT_IDENTITY = _load_alert_identity()
 
 
def teams_alert(level, title, event, msg, extra=None):
    """Best-effort Teams webhook alert. Never raises."""
    wh = CONFIG.get("teams", {}).get("webhook", "")
    if not wh:
        return
    try:
        d = _ALERT_IDENTITY
        body = json.dumps({
            "level": level,
            "title": title,
            "service": "Dixell_collector",
            "event": event,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": socket.gethostname(),
            "device": {
                # device_name from schedule.json (e.g. BW35) is the useful ID;
                # hostname is a fallback (often just "raspberrypi").
                "device_id": d.get("device_name") or socket.gethostname(),
                "hostname": socket.gethostname(),
                "dev_eui": d.get("dev_eui"),
                "device_name": d.get("device_name"),
                "site": d.get("site"),
                "company": d.get("company"),
                "zone": d.get("zone"),
                "compressor": d.get("compressor"),
            },
            "message": msg,
            "extra": extra,
        }, separators=(",", ":")).encode()
        rq = urllib.request.Request(
            wh, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(rq, timeout=8)
    except Exception as e:
        LOG.error("teams: %s", e)
 
 
# =========================
# SQLite Model (EXACT from working collector.py / emitter.py)
# =========================
Base = declarative_base()
 
 
class QueueItem(Base):
    __tablename__ = "queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    dev_eui = Column(String(128), nullable=False)
    payload_json = Column(Text, nullable=False)
    sent = Column(Boolean, nullable=False, default=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    try_count = Column(Integer, nullable=False, default=0)
    last_error = Column(Text, nullable=True)
 
 
Index("ix_queue_sent_id", QueueItem.sent, QueueItem.id)
 
 
def init_db():
    db_path = CONFIG.get("database", {}).get("path", "./readings.db")
    db_path = Path(db_path)
    if not db_path.is_absolute():
        db_path = SCRIPT_DIR / db_path
    url = f"sqlite:///{db_path.resolve()}"
    engine = create_engine(url, echo=False, future=True,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, Session
 
 
# =========================
# Protocol Constants (exact from working collector.py / Dixell_final.py)
# =========================

 
# Excel Variable name -> MQTT/DB key
EXCEL_TO_MQTT = {
    "diF_C":            "dif_c",
    "Def_end_C":        "def_end_c",
    "Def_int_h":        "def_int_h",
    "Def_type":         "def_type",
    "Def_timeout_min":  "def_timeout_min",
    "Cell_probe_cal_C": "cell_probe_cal_c",
    "Evap_probe_cal_C": "evap_probe_cal_c",
    "Evap2_probe_cal_C": "evap2_probe_cal_c",
    "Alarm_Hi_C":       "alarm_high_temp",
    "Alarm_Lo_C":       "alarm_low_temp",
    "LSE_min_C":        "lse_min_c",
    "HSE_max_C":        "hse_max_c",
}

# Existing schedule/MQTT name -> Dixell profile parameter
DIXELL_WRITE_PARAMS = {
    "setpoint": "SEt",
    "dif_c": "Hy",
    "alarm_low_temp": "ALL",
    "alarm_high_temp": "ALU",
    "def_end_c": "dtE",
    "cell_probe_cal_c": "ot",
    "evap_probe_cal_c": "oE",
    "evap2_probe_cal_c": "o3",
    "def_type": "tdF",
    "def_int_h": "idF",
    "def_timeout_min": "MdF",
    "lse_min_c": "LS",
    "hse_max_c": "US",
}

# Which params to actually write (by mqtt_key)
WRITE_ENABLED = {
    "def_end_c": True, "def_int_h": True, "def_timeout_min": True,
    "cell_probe_cal_c": True, "evap_probe_cal_c": True, "evap2_probe_cal_c": True,
    "dif_c": True, "def_type": True,
    "alarm_low_temp": True, "alarm_high_temp": True,
    "lse_min_c": True, "hse_max_c": True,
}
 
  
# =========================
# Decode (EXACT from working collector.py)
# =========================
 
def decode_readings(
    values: Dict[str, Any],
    cached: Dict[str, Any],
) -> Dict[str, Any]:
    """Translate decoded Dixell values into existing payload names."""

    mapping = {
        dixell_name: mqtt_name
        for mqtt_name, dixell_name in DIXELL_WRITE_PARAMS.items()
    }

    mapping.update({
        "Pb1": "probe_temperature",
        "Pb2": "evap_temperature",
        "Pb3": "ai3_temp",
        "FSt": "fan_off_temp",
        "Fnd": "fan_delay",
        "Fdt": "drain_time",
        "dSd": "def_delay",
        "Compressor status": "compressor_on",
        "Defrost status": "defrost_active",
        "Fan status": "fan_on",
        "Alarm Status": "alarm_active",
    })

    readings = {}

    for dixell_name, mqtt_name in mapping.items():
        if dixell_name in values:
            readings[mqtt_name] = values[dixell_name]
        elif mqtt_name in cached:
            readings[mqtt_name] = cached[mqtt_name]
            LOG.debug("Using cached value for %s", mqtt_name)

    return readings
 
 
# =========================
# SharePoint Fetcher
# =========================
 
def fetch_sharepoint_config() -> Optional[Dict]:
    if not SHAREPOINT_OK:
        LOG.warning("requests/pandas not installed")
        return None
    sp = CONFIG.get("sharepoint", {})
    if not sp.get("enabled", False):
        return None
    tenant_id = sp.get("tenant_id", "")
    client_id = sp.get("client_id", "")
    client_secret = sp.get("client_secret", "")
    if not tenant_id or not client_id or not client_secret:
        LOG.warning("SharePoint credentials missing")
        return None
    try:
        LOG.info("Fetching schedule from SharePoint...")
        token_resp = requests.post(
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            data={"client_id": client_id, "client_secret": client_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"}, timeout=10)
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
 
        hostname = sp.get("hostname", "")
        site_name = sp.get("site_name", "")
        site_resp = requests.get(
            f"https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{site_name}",
            headers=headers, timeout=10)
        site_resp.raise_for_status()
        site_id = site_resp.json()["id"]
 
        drive_resp = requests.get(
            f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
            headers=headers, timeout=10)
        drive_resp.raise_for_status()
        drive_id = drive_resp.json()["id"]
 
        from urllib.parse import quote
        file_path = sp.get("file_path", "/schedule.xlsx")
        file_resp = requests.get(
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}/root:{quote(file_path, safe='/')}:/content",
            headers=headers, timeout=30)
        file_resp.raise_for_status()
        LOG.info("Downloaded %d bytes", len(file_resp.content))
 
        sheet_name = sp.get("sheet_name", "")
        xlsx = pd.ExcelFile(BytesIO(file_resp.content))
        if sheet_name not in xlsx.sheet_names:
            LOG.error("Sheet '%s' not found. Available: %s", sheet_name, xlsx.sheet_names)
            return None
 
        df = pd.read_excel(xlsx, sheet_name=sheet_name)
        config = _parse_excel(df)
 
        # Carry over _meta from schedule.json so dev_eui etc. are available
        try:
            baseline = load_schedule()
            config["_meta"] = baseline.get("_meta", {}).copy()
            config["_meta"]["source"] = "sharepoint"
            config["_meta"]["fetched_at"] = datetime.now(timezone.utc).isoformat()
        except Exception:
            config["_meta"] = {"source": "sharepoint",
                               "fetched_at": datetime.now(timezone.utc).isoformat()}
 
        LOG.info("Parsed config from sheet '%s'", sheet_name)
        return config
    except Exception as e:
        LOG.error("SharePoint fetch failed: %s", e)
        return None
 
 
def _parse_excel(df) -> dict:
    """Parse Excel into schedule.json format with MQTT/DB keys."""
    config = {"identity": {}, "schedule": {}, "parameters": {}}
    in_schedule = False
    for _, row in df.iterrows():
        var = str(row.get("Variable", "")).strip()
        val = row.get("Value")
        sub = row.get("Subvalue")
        if pd.isna(var) or var in ("", "nan"):
            if in_schedule and not pd.isna(val):
                try:
                    h = int(val)
                    sp = float(sub) if not pd.isna(sub) else None
                    if sp is not None and 0 <= h <= 23:
                        config["schedule"][str(h)] = sp
                except (ValueError, TypeError):
                    pass
            continue
        if var in ("Company", "Site", "Zone", "Compressor", "Pi"):
            config["identity"][var.lower()] = val
            continue
        if var == "Schedule":
            in_schedule = True
            continue
        if in_schedule:
            in_schedule = False
        if var in EXCEL_TO_MQTT:
            mqtt_key = EXCEL_TO_MQTT[var]
            try:
                config["parameters"][mqtt_key] = float(val) if not pd.isna(val) else None
            except (ValueError, TypeError):
                config["parameters"][mqtt_key] = val
    return config
 
 
# =========================
# Config Manager
# =========================


def get_schedule() -> Optional[Dict]:
    """SharePoint -> live_config.json -> schedule.json"""
    sync = load_json_safe(SYNC_STATE_FILE) or {"consecutive_failures": 0}
    failures = sync.get("consecutive_failures", 0)
    threshold = CONFIG.get("offline_failure_threshold", 3)
 
    config = fetch_sharepoint_config()
    if config:
        save_json_safe(LIVE_CONFIG_FILE, config)
        save_json_safe(SYNC_STATE_FILE, {"consecutive_failures": 0})
        LOG.info("Using fresh SharePoint config")
        return config
 
    failures += 1
    save_json_safe(SYNC_STATE_FILE, {"consecutive_failures": failures})
    LOG.warning("SharePoint failed (%d/%d)", failures, threshold)
 
    if failures >= threshold:
        try:
            LOG.warning("FALLBACK to schedule.json (offline threshold)")
            return load_schedule()
        except FileNotFoundError:
            pass
 
    live = load_json_safe(LIVE_CONFIG_FILE)
    if live:
        LOG.info("Using cached live_config.json")
        return live
 
    try:
        return load_schedule()
    except FileNotFoundError:
        LOG.error("No config available!")
        return None
 
 
# =========================
# Payload Builder (matches unified_controller format for DB)
# =========================
 
def build_payload(schedule: Dict, readings: Dict, ts: datetime) -> Dict:
    identity = schedule.get("identity", {})
    meta = schedule.get("_meta", {})
    payload = {
        "applicationID": 1,
        "devEUI": meta.get("dev_eui", "unknown"),
        "deviceName": meta.get("device_name", "unknown"),
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "message": 0,  # Set by caller
        "company": identity.get("company", ""),
        "site": identity.get("site", ""),
        "zone": identity.get("zone", 0),
        "compressor": identity.get("compressor", 0),
    }
    for k, v in readings.items():
        if v is not None:
            payload[k] = v
    return payload
 
 
# =========================
# Main Collection
# =========================
 
def collect() -> int:
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(TZ)
    current_hour = str(now_local.hour)
 
    LOG.info("=" * 60)
    LOG.info("Collector start  hour=%s", current_hour)
    LOG.info("=" * 60)
 
    # --- 1. Get schedule ---
    schedule = get_schedule()
    if not schedule:
        LOG.error("No schedule available")
        return 2
 
    meta = schedule.get("_meta", {})
    dev_eui = meta.get("dev_eui", "unknown")
 
    # Refresh alert identity from the resolved schedule (SharePoint/live may
    # differ from the on-disk baseline loaded at import time).
    idy = schedule.get("identity", {})
    _ALERT_IDENTITY.update({
        "dev_eui": dev_eui,
        "device_name": meta.get("device_name", _ALERT_IDENTITY.get("device_name")),
        "site": idy.get("site", _ALERT_IDENTITY.get("site")),
        "company": idy.get("company", _ALERT_IDENTITY.get("company")),
        "zone": idy.get("zone", _ALERT_IDENTITY.get("zone")),
        "compressor": idy.get("compressor", _ALERT_IDENTITY.get("compressor")),
    })
 
    # Target setpoint
    hourly = schedule.get("schedule", {})
    target_sp = hourly.get(current_hour)
    if target_sp is None:
        target_sp = hourly.get(int(current_hour))
    if target_sp is not None:
        target_sp = float(target_sp)
        LOG.info("Target setpoint hour %s: %.1f°C", current_hour, target_sp)
    else:
        LOG.warning("No setpoint for hour %s, using fallback", current_hour)
        target_sp = schedule.get("parameters", {}).get("setpoint", CONFIG.get("fallback_setpoint", 5.0))
 
    # Target params
    target_params = {}
    for mqtt_key, val in schedule.get("parameters", {}).items():
        if val is not None and mqtt_key in DIXELL_WRITE_PARAMS and WRITE_ENABLED.get(mqtt_key, False):
            target_params[mqtt_key] = float(val)

    if "dif_c" not in target_params:
        target_params["dif_c"] = float(
            CONFIG.get("fallback_dif_c", 2.0)
        )

    if target_params:
        LOG.info("Target params: %s", target_params)
 
    # --- 2. Cached readings + last setpoint ---
    cached = load_json_safe(CACHE_FILE) or {}
    last_sp_state = load_json_safe(LAST_SP_FILE) or {}
    last_sp = last_sp_state.get("last_setpoint_c")
 
    # --- 3. Init DB ---
    engine, Session = init_db()
 
    # --- 4. Connect ---
    device = DixellXR77U(
        SCRIPT_DIR / CONFIG.get("profile", "xr77u.json"),
        CONFIG.get("serial", {}),
    )
    port = device.connection["port"]
    unit = int(device.connection["slave"])
    try:
        device.connect()
        LOG.info("Connected to %s", port)
    except Exception as e:
        LOG.error("Connection failed: %s", e)
        teams_alert("error", "Dixell serial connect failed", "serial_connect_failed",
                    str(e), extra={"port": port, "unit": unit})
        return 1
 
    try:
        # --- 5. Do ALL writes first ---
        # Write the scheduled setpoint using its Dixell profile mapping.
        if not device.write_parameter("SEt", target_sp):
            LOG.warning("Setpoint write failed, continuing")
            teams_alert(
                "error",
                "Dixell setpoint write failed",
                "setpoint_write_failed",
                "Controller read-back did not match the target setpoint.",
                extra={
                    "target_setpoint": target_sp,
                    "port": port,
                    "unit": unit,
                },
            )

        time.sleep(0.2)
 
        # Write changed params (compare against cached values from last run)
        params_written = {}
        params_failed = []
        for mqtt_key, target_val in target_params.items():
            cur = cached.get(mqtt_key)
            parameter_name = DIXELL_WRITE_PARAMS[mqtt_key]
            parameter = device.parameters[parameter_name]
            scale = float(parameter.get("scale", 1))
 
            if cur is None:
                need = True  # Not in cache, write to be safe
            elif scale > 1:
                need = abs(float(target_val) - float(cur)) >= 0.05
            else:
                need = int(target_val) != int(cur)
 
            if not need:
                continue
 
            LOG.info("Param %s: cached=%s target=%s -> writing", mqtt_key, cur, target_val)
            if device.write_parameter(parameter_name, target_val):
                params_written[mqtt_key] = {"from": cur, "to": target_val}
                time.sleep(0.2)
            else:
                LOG.error("Write failed: %s", mqtt_key)
                params_failed.append(mqtt_key)
 
        if params_failed:
            teams_alert("error", "Dixell param write failed", "param_write_failed",
                        "Controller read-back did not match one or more parameter writes.",
                        extra={"failed_params": params_failed, "port": port, "unit": unit})
 
        if params_written:
            LOG.info("Wrote params: %s", list(params_written.keys()))
        else:
            LOG.info("No param changes needed")
 
        # --- 6. Read the mapped Dixell registers and coils ---
        time.sleep(1.0)

        values, raw_values, read_errors = device.read_all()

        polled = decode_readings(values, cached) if values else {}

        LOG.info(
            "Read %d Dixell values -> %d payload readings",
            len(values),
            len(polled),
        )

        for parameter_name, error in read_errors.items():
            LOG.warning(
                "Dixell read failed for %s: %s",
                parameter_name,
                error,
            )

        if not values:
            teams_alert(
                "error",
                "Dixell poll returned nothing",
                "poll_empty",
                "No mapped values were read from the controller.",
                extra={"port": port, "unit": unit},
            )
 
        # Log what the controller actually reports vs what we wrote
        if polled.get("setpoint") is not None:
            polled_sp = polled["setpoint"]
            if abs(polled_sp - target_sp) > 0.15:
                LOG.warning("SETPOINT MISMATCH: wrote %.1f but controller reports %.1f",
                            target_sp, polled_sp)
                teams_alert("warning", "Dixell setpoint mismatch", "setpoint_mismatch",
                            "Controller readback does not match the written setpoint.",
                            extra={"wrote": target_sp, "controller_reports": polled_sp})
            else:
                LOG.info("Setpoint confirmed: controller reports %.1f (wrote %.1f)",
                         polled_sp, target_sp)
        else:
            LOG.warning("Setpoint not in poll response")
            teams_alert("warning", "Dixell setpoint not in poll", "setpoint_not_polled",
                        "Setpoint was absent from the Modbus readings.",
                        extra={"port": port, "unit": unit})
 
        # --- 7. Build complete readings from poll + cache ---
        # ONLY report what the controller actually has.
        # For missing tokens, use cached values (from last successful poll).
        # NEVER overlay with target/written values — that lies to the DB.
        readings = cached.copy()
        readings.update(polled)
 
        LOG.info("Final readings: %d fields (%d polled, %d from cache)",
                 len(readings), len(polled), len(readings) - len(polled))
 
        # --- 8. Determine status code ---
        sp_written = False
        if last_sp is not None and abs(target_sp - last_sp) >= 0.05:
            sp_written = True
        elif last_sp is None:
            sp_written = True
 
        if sp_written or params_written:
            code = 200
        elif last_sp is None:
            code = 300
        elif readings.get("setpoint") is not None and abs(readings["setpoint"] - last_sp) > 0.05:
            code = 300
        else:
            code = 201
 
        # --- 9. Save state ---
        current_sp = readings.get("setpoint", target_sp)
        save_json_safe(CACHE_FILE, readings)
        save_json_safe(LAST_SP_FILE, {"last_setpoint_c": current_sp,
                                       "ts": now_utc.isoformat()})
 
        # --- 10. Build payload & queue to SQLite ---
        payload = build_payload(schedule, readings, now_utc)
        payload["message"] = code
        if sp_written:
            payload["setpoint_written"] = True
            payload["setpoint_target"] = target_sp
        if params_written:
            payload["params_written"] = params_written
 
        with Session() as s:
            item = QueueItem(
                created_at=now_utc,
                dev_eui=dev_eui,
                payload_json=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                sent=False,
                try_count=0,
            )
            s.add(item)
            s.commit()
            LOG.info("Queued id=%d  status=%d  fields=%d", item.id, code, len(readings))
 
        return 0
 
    except Exception as e:
        LOG.error("Error: %s", e)
        import traceback
        traceback.print_exc()
        teams_alert("error", "Dixell collector error", "collect_exception",
                    str(e), extra={"traceback": traceback.format_exc()})
        return 1
 
    finally:
        device.close()
 
 
if __name__ == "__main__":
    lockfile = open(SCRIPT_DIR / "dixell_collector.lock", "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.info("Another collector instance running")
        sys.exit(0)
 
    try:
        rc = collect()
    except Exception as e:
        import traceback
        LOG.error("Fatal: %s", e)
        traceback.print_exc()
        teams_alert("critical", "Dixell collector fatal", "fatal_exception",
                    str(e), extra={"traceback": traceback.format_exc()})
        rc = 1
    sys.exit(rc)
