#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAREL PJEZ Queue Emitter - Publishes queued readings to MQTT.

Run as cron every 5 minutes:
  */5 * * * * /path/to/venv/bin/python /path/to/emitter.py

Or for one-shot:
  python emitter.py
"""

import os
import sys
import json
import ssl
import time
import base64
import socket
import logging
import tempfile
import stat
import subprocess
import threading
import fcntl
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List

import yaml
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    Index,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from paho.mqtt.client import Client, MQTTv5
from paho.mqtt.properties import Properties
from paho.mqtt.packettypes import PacketTypes

try:
    from paho.mqtt.client import CallbackAPIVersion
    CB_VER = CallbackAPIVersion.VERSION2
except:
    CB_VER = None

# =========================
# Configuration
# =========================
SCRIPT_DIR = Path(__file__).parent.resolve()


def load_config() -> Dict[str, Any]:
    config_path = SCRIPT_DIR / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError("config.yaml not found")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()

# =========================
# Logging
# =========================
log_level = CONFIG.get("logging", {}).get("level", "INFO").upper()
LOG = logging.getLogger("emitter")
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)

# =========================
# SQLite Model
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
    engine = create_engine(
        url, echo=False, future=True,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine, Session


# =========================
# Connectivity Check
# =========================
def internet_ok() -> bool:
    ping_host = CONFIG.get("connectivity", {}).get("ping_host", "1.1.1.1")
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ping_host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return r.returncode == 0
    except:
        return False


# =========================
# TLS Setup
# =========================
def _decode_b64(s: str) -> bytes:
    return base64.b64decode((s or "").strip(), validate=False)


def _ensure_nl(b: bytes) -> bytes:
    return b if b.endswith(b"\n") else b + b"\n"


def build_tls_context(mqtt_cfg: Dict[str, Any]):
    client_cert_b64 = mqtt_cfg.get("client_cert", "").strip()
    client_key_b64 = mqtt_cfg.get("client_key", "").strip()
    ca_cert_b64 = mqtt_cfg.get("ca_cert", "").strip()

    if not client_cert_b64 or not client_key_b64:
        raise ValueError("client_cert and client_key required for TLS")

    crt_raw = _decode_b64(client_cert_b64)
    key_raw = _decode_b64(client_key_b64)

    tmpdir = tempfile.TemporaryDirectory()

    cert_path = os.path.join(tmpdir.name, "client.crt")
    key_path = os.path.join(tmpdir.name, "client.key")

    with open(cert_path, "wb") as f:
        f.write(_ensure_nl(crt_raw))
    with open(key_path, "wb") as f:
        f.write(_ensure_nl(key_raw))

    try:
        os.chmod(cert_path, stat.S_IRUSR)
        os.chmod(key_path, stat.S_IRUSR)
    except:
        pass

    ca_path = None
    if ca_cert_b64:
        ca_raw = _decode_b64(ca_cert_b64)
        ca_path = os.path.join(tmpdir.name, "ca.crt")
        with open(ca_path, "wb") as f:
            f.write(_ensure_nl(ca_raw))

    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_alpn_protocols(["mqtt"])
    except:
        pass

    if ca_path:
        ctx.load_verify_locations(cafile=ca_path)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)

    return ctx, tmpdir


def _rc_str(rc):
    m = {0: "Success", 128: "Unspecified error", 133: "Client ID not valid",
         134: "Bad credentials", 135: "Not authorized"}
    return m.get(rc, str(rc))


# =========================
# MQTT Publishing
# =========================
def publish_payload(mqtt_cfg: Dict[str, Any], payload: Dict[str, Any]) -> bool:
    host = mqtt_cfg.get("host", "")
    port = mqtt_cfg.get("port", 8883)
    topic = mqtt_cfg.get("topic", "")
    client_id = mqtt_cfg.get("client_id", "")
    qos = mqtt_cfg.get("qos", 1)

    if not host or not topic:
        LOG.error("MQTT host and topic required")
        return False

    if CB_VER:
        client = Client(client_id=client_id, protocol=MQTTv5, callback_api_version=CB_VER)
    else:
        client = Client(client_id=client_id, protocol=MQTTv5)

    username = mqtt_cfg.get("username", "") or client_id
    if username:
        client.username_pw_set(username=username, password=mqtt_cfg.get("password"))

    tls_enabled = mqtt_cfg.get("tls_enabled", True)
    tmpdir = None

    if tls_enabled:
        try:
            tls_ctx, tmpdir = build_tls_context(mqtt_cfg)
            client.tls_set_context(tls_ctx)
        except Exception as e:
            LOG.error("TLS setup failed: %s", e)
            return False

    connected_evt = threading.Event()
    last_rc = {"rc": None}

    def on_connect(cli, userdata, flags, reason_code, properties):
        rc = reason_code.value if hasattr(reason_code, "value") else int(reason_code)
        last_rc["rc"] = rc
        if rc == 0:
            connected_evt.set()

    client.on_connect = on_connect

    connect_props = Properties(PacketTypes.CONNECT)
    connect_props.SessionExpiryInterval = 0

    try:
        client.connect(host, port, keepalive=60, properties=connect_props)
        client.loop_start()

        if not connected_evt.wait(10):
            LOG.error("MQTT connect failed rc=%s", _rc_str(last_rc["rc"]))
            return False

        body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        pub_props = Properties(PacketTypes.PUBLISH)
        pub_props.ContentType = "application/json"

        info = client.publish(topic, body_bytes, qos=qos, properties=pub_props)
        info.wait_for_publish()

        if info.rc != 0:
            LOG.error("Publish failed rc=%s", info.rc)
            return False

        return True

    except Exception as e:
        LOG.error("MQTT error: %s", e)
        return False

    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except:
            pass
        if tmpdir:
            try:
                tmpdir.cleanup()
            except:
                pass


# =========================
# Main Emission
# =========================
def emit() -> int:
    mqtt_cfg = CONFIG.get("mqtt", {})

    if not mqtt_cfg.get("host") or not mqtt_cfg.get("topic"):
        LOG.error("MQTT host and topic must be configured")
        return 2

    if not internet_ok():
        LOG.info("Internet not available, skipping")
        return 0

    engine, Session = init_db()
    batch_limit = CONFIG.get("emission", {}).get("batch_limit", 100)
    sent_count = 0

    with Session() as s:
        rows: List[QueueItem] = (
            s.query(QueueItem)
            .filter(QueueItem.sent == False)
            .order_by(QueueItem.id.asc())
            .limit(batch_limit)
            .all()
        )

        if not rows:
            LOG.info("No queued readings")
            return 0

        LOG.info("Found %d unsent readings", len(rows))

        for r in rows:
            if sent_count > 0 and sent_count % 10 == 0:
                if not internet_ok():
                    LOG.info("Internet dropped after %d sends", sent_count)
                    return 0

            try:
                payload = json.loads(r.payload_json)

                if publish_payload(mqtt_cfg, payload):
                    r.sent = True
                    r.sent_at = datetime.now(timezone.utc)
                    r.try_count = (r.try_count or 0) + 1
                    r.last_error = None
                    s.commit()

                    sent_count += 1
                    LOG.info("Sent id=%d devEUI=%s (total=%d)", r.id, r.dev_eui, sent_count)
                else:
                    r.try_count = (r.try_count or 0) + 1
                    r.last_error = "publish_failed"
                    s.commit()
                    LOG.warning("Failed id=%d, stopping", r.id)
                    return 1

            except Exception as e:
                r.try_count = (r.try_count or 0) + 1
                r.last_error = str(e)
                s.commit()
                LOG.error("Error id=%d: %s", r.id, e)
                return 1

    LOG.info("Emission complete. Sent %d readings.", sent_count)
    return 0


if __name__ == "__main__":
    lockfile = open(SCRIPT_DIR / "dixell_emitter.lock", "w")
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)

    sys.exit(emit())
