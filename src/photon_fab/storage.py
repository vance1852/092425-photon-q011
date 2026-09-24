"""芯片批次、测量记录与供应商物料追溯的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS suppliers(
 supplier_id TEXT PRIMARY KEY, name TEXT NOT NULL,
 material_types TEXT NOT NULL DEFAULT '', contact TEXT NOT NULL DEFAULT '',
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_batches(
 batch_id TEXT PRIMARY KEY, supplier_id TEXT NOT NULL REFERENCES suppliers(supplier_id),
 material_type TEXT NOT NULL, spec TEXT NOT NULL, quantity REAL NOT NULL,
 received_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'registered',
 registered_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS material_inspections(
 inspection_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES material_batches(batch_id),
 result TEXT NOT NULL, findings TEXT NOT NULL DEFAULT '', metrics TEXT NOT NULL DEFAULT '{}',
 inspector TEXT NOT NULL, inspected_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS substitution_approvals(
 substitution_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 material_type TEXT NOT NULL, intended_spec TEXT NOT NULL,
 substitute_batch_id TEXT NOT NULL REFERENCES material_batches(batch_id),
 reason TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', reviewer TEXT, decision_reason TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS lot_materials(
 link_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 batch_id TEXT NOT NULL REFERENCES material_batches(batch_id),
 role TEXT NOT NULL, quantity REAL NOT NULL,
 substitution_id TEXT REFERENCES substitution_approvals(substitution_id),
 linked_by TEXT NOT NULL, linked_at TEXT NOT NULL,
 UNIQUE(lot_id,batch_id));
CREATE INDEX IF NOT EXISTS idx_lot_materials_batch ON lot_materials(batch_id);
CREATE TABLE IF NOT EXISTS trace_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL,
 entity_id TEXT NOT NULL, event_type TEXT NOT NULL, actor TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trace_events_entity ON trace_events(entity_type,entity_id);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))


def trace_event(db: sqlite3.Connection, entity_type: str, entity_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO trace_events(entity_type,entity_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?,?)",
        (entity_type, entity_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()),
    )
