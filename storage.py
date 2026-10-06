"""SQLite storage, with optional backup of submitted bookings to a private Hugging Face dataset."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from agent import IST

log = logging.getLogger("storage")

DATA_DIR = Path(os.getenv("DATA_DIR") or ("/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bookings.db"
SYNC_DIR = DATA_DIR / "hf_sync"
SYNC_DIR.mkdir(exist_ok=True)

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE,
    ref TEXT,
    status TEXT DEFAULT 'in_progress',     -- in_progress | submitted | booked | cancelled | abandoned
    lang TEXT,
    data_json TEXT,
    transcript_json TEXT,
    whatsapp_status TEXT,
    admin_note TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON bookings(status);
""")
_conn.commit()

# ---------------------------------------------------------------- HF dataset backup (optional)
_scheduler = None
HF_DATASET = os.getenv("HF_DATASET_REPO")          # e.g. "your-username/rail-bookings" (kept private)
if HF_DATASET and os.getenv("HF_TOKEN"):
    try:
        from huggingface_hub import CommitScheduler, create_repo
        create_repo(HF_DATASET, repo_type="dataset", private=True, exist_ok=True, token=os.getenv("HF_TOKEN"))
        _scheduler = CommitScheduler(repo_id=HF_DATASET, repo_type="dataset", folder_path=SYNC_DIR,
                                     path_in_repo="bookings", every=5, private=True, token=os.getenv("HF_TOKEN"))
        log.info("Backing up bookings to HF dataset %s", HF_DATASET)
    except Exception as e:  # never block the app because backup failed
        log.warning("HF dataset backup disabled: %s", e)
_sync_file = SYNC_DIR / f"bookings-{uuid.uuid4().hex[:8]}.jsonl"


def now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def upsert_draft(session_id: str, lang: str, data: dict, transcript: list, status: str = "in_progress") -> int:
    with _lock:
        cur = _conn.execute("SELECT id FROM bookings WHERE session_id=?", (session_id,))
        row = cur.fetchone()
        payload = (status, lang, json.dumps(data, ensure_ascii=False), json.dumps(transcript, ensure_ascii=False), now())
        if row:
            _conn.execute("UPDATE bookings SET status=?, lang=?, data_json=?, transcript_json=?, updated_at=? WHERE id=?",
                          (*payload, row["id"]))
            bid = row["id"]
        else:
            cur = _conn.execute(
                "INSERT INTO bookings (session_id, status, lang, data_json, transcript_json, updated_at, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (session_id, *payload, now()))
            bid = cur.lastrowid
        _conn.commit()
        return bid


def submit(session_id: str, lang: str, data: dict, transcript: list) -> dict:
    bid = upsert_draft(session_id, lang, data, transcript, status="submitted")
    ref = f"RB{datetime.now(IST).strftime('%d%m')}{bid:04d}"
    with _lock:
        _conn.execute("UPDATE bookings SET ref=? WHERE id=?", (ref, bid))
        _conn.commit()
    record = get(bid)
    if _scheduler is not None:
        try:
            with _scheduler.lock, _sync_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("HF backup write failed: %s", e)
    return record


def set_whatsapp_status(bid: int, status: str):
    with _lock:
        _conn.execute("UPDATE bookings SET whatsapp_status=? WHERE id=?", (status[:200], bid))
        _conn.commit()


def set_status(bid: int, status: str, note: str | None = None):
    with _lock:
        _conn.execute("UPDATE bookings SET status=?, admin_note=COALESCE(?, admin_note), updated_at=? WHERE id=?",
                      (status, note, now(), bid))
        _conn.commit()


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json") or "{}")
    d["transcript"] = json.loads(d.pop("transcript_json") or "[]")
    return d


def get(bid: int) -> dict | None:
    r = _conn.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    return _row(r) if r else None


def list_bookings(status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        rows = _conn.execute("SELECT * FROM bookings WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
    else:
        rows = _conn.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows.fetchall()]
