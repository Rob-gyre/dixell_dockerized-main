#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_queue.py — Inspect the local CAREL readings queue (readings.db).

Shows how many readings were COLLECTED and EMITTED, broken down by day,
plus overall totals, the backlog of unsent rows, and any errors. Use it to
check the daily state of the local DB at a glance.

Usage:
    python view_queue.py                 # summary + 7-day breakdown + last 5 payloads
    python view_queue.py --days 30       # 30-day daily breakdown
    python view_queue.py --today         # just today's numbers
    python view_queue.py --tail 10       # show last 10 full payloads
    python view_queue.py --unsent        # list every unsent row
    python view_queue.py --errors        # list rows that have a last_error
    python view_queue.py --payloads 3    # include 3 latest payloads in summary

Timestamps are stored in UTC and displayed in the configured timezone
(from config.yaml -> timezone, default Africa/Accra).
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, date
from collections import defaultdict

import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

SCRIPT_DIR = Path(__file__).parent.resolve()


# ── Config / DB ──────────────────────────────────────────────────────────────
def load_config() -> dict:
    p = SCRIPT_DIR / "config.yaml"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


CONFIG = load_config()


def get_local_tz():
    tzname = CONFIG.get("timezone") or CONFIG.get("carel", {}).get("timezone") or "Africa/Accra"
    if ZoneInfo:
        try:
            return ZoneInfo(tzname), tzname
        except Exception:
            pass
    return timezone.utc, "UTC"


LOCAL_TZ, LOCAL_TZ_NAME = get_local_tz()


def db_path() -> Path:
    p = Path(CONFIG.get("database", {}).get("path", "./readings.db"))
    if not p.is_absolute():
        p = SCRIPT_DIR / p
    return p.resolve()


def get_session():
    p = db_path()
    if not p.exists():
        print(f"ERROR: database not found at {p}")
        sys.exit(1)
    engine = create_engine(f"sqlite:///{p}", future=True)
    return sessionmaker(bind=engine, future=True), p


# ── Helpers ──────────────────────────────────────────────────────────────────
def parse_ts(value):
    """Parse a stored timestamp into an aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        # SQLAlchemy may store with a space or 'T'; normalise
        s = s.replace(" ", "T", 1) if "T" not in s and " " in s else s
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # last resort: strip fractional/zone oddities
            try:
                dt = datetime.fromisoformat(s.split(".")[0])
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt):
    return dt.astimezone(LOCAL_TZ) if dt else None


def local_day(dt):
    loc = to_local(dt)
    return loc.date() if loc else None


def fmt_local(dt):
    loc = to_local(dt)
    return loc.strftime("%Y-%m-%d %H:%M:%S") if loc else "-"


def bar(n, width=20, scale=1):
    filled = min(width, int(round(n / scale))) if scale else 0
    return "█" * filled


# ── Data load ────────────────────────────────────────────────────────────────
def load_rows(session):
    sql = text("SELECT id, created_at, dev_eui, payload_json, sent, sent_at, "
               "try_count, last_error FROM queue ORDER BY id ASC")
    rows = []
    for r in session.execute(sql).fetchall():
        rows.append({
            "id": r.id,
            "created_at": parse_ts(r.created_at),
            "dev_eui": r.dev_eui,
            "payload_json": r.payload_json,
            "sent": bool(r.sent),
            "sent_at": parse_ts(r.sent_at),
            "try_count": r.try_count or 0,
            "last_error": r.last_error,
        })
    return rows


# ── Views ────────────────────────────────────────────────────────────────────
def print_header(title):
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def show_summary(rows):
    total = len(rows)
    sent = sum(1 for r in rows if r["sent"])
    unsent = total - sent
    errored = sum(1 for r in rows if r["last_error"])
    retried = sum(1 for r in rows if r["try_count"] > 1)

    print_header("LOCAL DB SUMMARY")
    print(f"  Database : {db_path()}")
    print(f"  Timezone : {LOCAL_TZ_NAME}")
    print(f"  Devices  : {', '.join(sorted({r['dev_eui'] for r in rows})) or '-'}")
    print()
    print(f"  Total collected : {total}")
    print(f"  Emitted (sent)  : {sent}")
    print(f"  Pending (unsent): {unsent}")
    print(f"  With errors     : {errored}")
    print(f"  Retried (>1 try): {retried}")

    if rows:
        first = min((r["created_at"] for r in rows if r["created_at"]), default=None)
        last = max((r["created_at"] for r in rows if r["created_at"]), default=None)
        print(f"  Oldest reading  : {fmt_local(first)}")
        print(f"  Newest reading  : {fmt_local(last)}")


def show_daily(rows, days):
    """Per-day: collected (by created_at) and emitted (by sent_at)."""
    collected = defaultdict(int)
    emitted = defaultdict(int)

    for r in rows:
        d = local_day(r["created_at"])
        if d:
            collected[d] += 1
        if r["sent"]:
            sd = local_day(r["sent_at"])
            if sd:
                emitted[sd] += 1

    all_days = sorted(set(collected) | set(emitted), reverse=True)
    if days:
        all_days = all_days[:days]

    print_header(f"DAILY BREAKDOWN (last {len(all_days)} active day(s), {LOCAL_TZ_NAME})")
    if not all_days:
        print("  No dated rows.")
        return

    peak = max([collected[d] for d in all_days] + [emitted[d] for d in all_days] + [1])
    scale = max(1, peak / 20)

    print(f"  {'Date':<12} {'Collected':>9} {'Emitted':>8} {'Pending':>8}  Collected")
    print(f"  {'-'*12} {'-'*9} {'-'*8} {'-'*8}  {'-'*20}")
    tot_c = tot_e = 0
    for d in all_days:
        c = collected.get(d, 0)
        e = emitted.get(d, 0)
        pend = c - e
        tot_c += c
        tot_e += e
        print(f"  {d.isoformat():<12} {c:>9} {e:>8} {pend:>8}  {bar(c, scale=scale)}")
    print(f"  {'-'*12} {'-'*9} {'-'*8} {'-'*8}")
    print(f"  {'TOTAL':<12} {tot_c:>9} {tot_e:>8} {tot_c - tot_e:>8}")
    print("\n  Note: 'Emitted' is counted on the day it was SENT, which may")
    print("        differ from the day it was collected (e.g. offline backlog).")


def show_today(rows):
    today = datetime.now(LOCAL_TZ).date()
    c = sum(1 for r in rows if local_day(r["created_at"]) == today)
    e = sum(1 for r in rows if r["sent"] and local_day(r["sent_at"]) == today)
    print_header(f"TODAY ({today.isoformat()}, {LOCAL_TZ_NAME})")
    print(f"  Collected today : {c}")
    print(f"  Emitted today   : {e}")
    print(f"  Pending overall : {sum(1 for r in rows if not r['sent'])}")


def show_unsent(rows):
    unsent = [r for r in rows if not r["sent"]]
    print_header(f"UNSENT ROWS ({len(unsent)})")
    if not unsent:
        print("  None — queue fully drained.")
        return
    for r in unsent:
        print(f"  id={r['id']:<6} created={fmt_local(r['created_at'])} "
              f"tries={r['try_count']} err={r['last_error'] or '-'}")


def show_errors(rows):
    errs = [r for r in rows if r["last_error"]]
    print_header(f"ROWS WITH ERRORS ({len(errs)})")
    if not errs:
        print("  None.")
        return
    for r in errs:
        print(f"  id={r['id']:<6} sent={r['sent']} tries={r['try_count']} "
              f"created={fmt_local(r['created_at'])}")
        print(f"         error: {r['last_error']}")


def show_payloads(rows, n):
    latest = rows[-n:][::-1]
    print_header(f"LAST {len(latest)} PAYLOAD(S)")
    for r in latest:
        print(f"\n--- id={r['id']} sent={r['sent']} "
              f"created={fmt_local(r['created_at'])} "
              f"sent_at={fmt_local(r['sent_at'])} ---")
        try:
            print(json.dumps(json.loads(r["payload_json"]), indent=2))
        except Exception:
            print(r["payload_json"])


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Inspect the CAREL readings queue / local DB state")
    ap.add_argument("--days", type=int, default=7, help="Days in the daily breakdown (default 7)")
    ap.add_argument("--today", action="store_true", help="Show only today's numbers")
    ap.add_argument("--tail", type=int, default=0, help="Show last N full payloads")
    ap.add_argument("--payloads", type=int, default=0, help="Include N latest payloads in the summary view")
    ap.add_argument("--unsent", action="store_true", help="List all unsent rows")
    ap.add_argument("--errors", action="store_true", help="List rows with errors")
    args = ap.parse_args()

    Session, _ = get_session()
    with Session() as s:
        rows = load_rows(s)

    if args.today:
        show_summary(rows)
        show_today(rows)
        return

    if args.tail:
        show_payloads(rows, args.tail)
        return

    # Default dashboard
    show_summary(rows)
    show_daily(rows, args.days)

    if args.unsent:
        show_unsent(rows)
    if args.errors:
        show_errors(rows)
    if args.payloads:
        show_payloads(rows, args.payloads)


if __name__ == "__main__":
    main()
