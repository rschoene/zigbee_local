"""SQLite persistence layer shared by the daemon and the CLI.

Schema
------
- ``devices``   : every device a source has reported (with its known metrics).
- ``monitored`` : the registry of (source, device, metric) tuples we record.
- ``readings``  : the time series of readings for monitored metrics.

Everything is keyed by the composite ``(source_id, device_id)`` so multiple
sources can coexist even if they expose devices with the same id/name.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    source_id  TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    name       TEXT,
    metrics    TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT,
    left_at    TEXT,   -- NULL while the device is on the network; set when it leaves
    PRIMARY KEY (source_id, device_id)
);

CREATE TABLE IF NOT EXISTS source_status (
    source_id  TEXT PRIMARY KEY,
    status     TEXT NOT NULL,   -- 'ok' | 'error'
    detail     TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitored (
    source_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    metric    TEXT NOT NULL,
    added_at  TEXT NOT NULL,
    PRIMARY KEY (source_id, device_id, metric)
);

CREATE TABLE IF NOT EXISTS readings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    metric     TEXT NOT NULL,
    value      REAL,
    value_text TEXT,
    unit       TEXT,
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_id ON readings(id);
CREATE INDEX IF NOT EXISTS idx_readings_lookup ON readings(source_id, device_id, metric, id);

CREATE TABLE IF NOT EXISTS reporting_config (
    source_id    TEXT NOT NULL,
    device_id    TEXT NOT NULL,
    cluster_id   INTEGER NOT NULL,
    attr_id      INTEGER NOT NULL,
    min_interval INTEGER NOT NULL,
    max_interval INTEGER NOT NULL,
    reportable_change INTEGER,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (source_id, device_id, cluster_id, attr_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """A thin, thread-safe wrapper around a SQLite connection."""

    def __init__(self, path: str):
        path = str(path)
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets the CLI read while the daemon writes without locking.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Apply lightweight schema migrations to pre-existing databases."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(devices)")}
        if "left_at" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN left_at TEXT")
        if "metric_map" not in cols:
            self._conn.execute("ALTER TABLE devices ADD COLUMN metric_map TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # devices
    # ------------------------------------------------------------------ #
    def upsert_device(
        self,
        source_id: str,
        device_id: str,
        name: str | None = None,
        metrics: list[str] | None = None,
        metric_map: dict[str, list[int]] | None = None,
    ) -> None:
        metrics_json = json.dumps(metrics) if metrics is not None else None
        metric_map_json = json.dumps(metric_map) if metric_map is not None else None
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO devices(source_id, device_id, name, metrics, metric_map, first_seen, last_seen, left_at)
                VALUES(?,?,?,?,?,?,?,NULL)
                ON CONFLICT(source_id, device_id) DO UPDATE SET
                    name = COALESCE(excluded.name, devices.name),
                    metrics = COALESCE(excluded.metrics, devices.metrics),
                    metric_map = COALESCE(excluded.metric_map, devices.metric_map),
                    last_seen = excluded.last_seen,
                    left_at = NULL
                """,
                (source_id, device_id, name, metrics_json, metric_map_json, now, now),
            )

    def mark_device_left(self, source_id: str, device_id: str) -> None:
        """Record that a device left the network (sets left_at; no-op if unknown)."""
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE devices SET left_at = ? WHERE source_id = ? AND device_id = ?",
                (now, source_id, device_id),
            )

    def list_devices(self, source_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT source_id, device_id, name, metrics, metric_map, first_seen, last_seen, left_at FROM devices"
        args: list[Any] = []
        if source_id:
            q += " WHERE source_id = ?"
            args.append(source_id)
        q += " ORDER BY source_id, device_id"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "source_id": r["source_id"],
                    "device_id": r["device_id"],
                    "name": r["name"],
                    "metrics": json.loads(r["metrics"]) if r["metrics"] else [],
                    "metric_map": json.loads(r["metric_map"]) if r["metric_map"] else {},
                    "first_seen": r["first_seen"],
                    "last_seen": r["last_seen"],
                    "left_at": r["left_at"],
                }
            )
        return out

    # ------------------------------------------------------------------ #
    # source status
    # ------------------------------------------------------------------ #
    def set_source_status(self, source_id: str, status: str, detail: str | None = None) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO source_status(source_id, status, detail, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (source_id, status, detail, now),
            )

    def get_source_status(self, source_id: str | None = None) -> dict[str, dict[str, Any]]:
        q = "SELECT source_id, status, detail, updated_at FROM source_status"
        args: list[Any] = []
        if source_id:
            q += " WHERE source_id = ?"
            args.append(source_id)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return {r["source_id"]: dict(r) for r in rows}

    def devices_with_readings(self) -> set[tuple[str, str]]:
        """Return the set of (source_id, device_id) that have >=1 reading."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT source_id, device_id FROM readings"
            ).fetchall()
        return {(r["source_id"], r["device_id"]) for r in rows}

    # ------------------------------------------------------------------ #
    # monitored registry
    # ------------------------------------------------------------------ #
    def add_monitored(self, source_id: str, device_id: str, metrics: list[str]) -> None:
        now = _now()
        with self._lock, self._conn:
            for m in metrics:
                self._conn.execute(
                    "INSERT OR IGNORE INTO monitored(source_id, device_id, metric, added_at) VALUES(?,?,?,?)",
                    (source_id, device_id, m, now),
                )

    def remove_monitored(
        self, source_id: str, device_id: str, metrics: list[str] | None = None
    ) -> None:
        with self._lock, self._conn:
            if metrics:
                for m in metrics:
                    self._conn.execute(
                        "DELETE FROM monitored WHERE source_id=? AND device_id=? AND metric=?",
                        (source_id, device_id, m),
                    )
            else:
                self._conn.execute(
                    "DELETE FROM monitored WHERE source_id=? AND device_id=?",
                    (source_id, device_id),
                )

    def list_monitored(self, source_id: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT source_id, device_id, metric, added_at FROM monitored"
        args: list[Any] = []
        if source_id:
            q += " WHERE source_id = ?"
            args.append(source_id)
        q += " ORDER BY source_id, device_id, metric"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def monitored_set(self) -> set[tuple[str, str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, device_id, metric FROM monitored"
            ).fetchall()
        return {(r["source_id"], r["device_id"], r["metric"]) for r in rows}

    def is_monitored(self, source_id: str, device_id: str, metric: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM monitored WHERE source_id=? AND device_id=? AND metric=?",
                (source_id, device_id, metric),
            )
            return cur.fetchone() is not None

    # ------------------------------------------------------------------ #
    # readings
    # ------------------------------------------------------------------ #
    def insert_reading(self, r) -> None:
        ts = r.ts.isoformat() if hasattr(r.ts, "isoformat") else str(r.ts)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO readings(source_id, device_id, metric, value, value_text, unit, ts) "
                "VALUES(?,?,?,?,?,?,?)",
                (r.source_id, r.device_id, r.metric, r.value, r.value_text, r.unit, ts),
            )

    def max_reading_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM readings").fetchone()
        return int(row["m"])

    def readings_since(self, last_id: int, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source_id, device_id, metric, value, value_text, unit, ts "
                "FROM readings WHERE id > ? ORDER BY id ASC LIMIT ?",
                (last_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def metric_units(self, source_id: str | None = None) -> dict[tuple[str, str, str], str]:
        """Map (source_id, device_id, metric) -> most recent unit seen in readings."""
        where = "WHERE source_id = ?" if source_id else ""
        args: list[Any] = [source_id] if source_id else []
        q = f"""
            SELECT r.source_id, r.device_id, r.metric, r.unit
            FROM readings r
            JOIN (
                SELECT source_id, device_id, metric, MAX(id) AS max_id
                FROM readings
                {where}
                GROUP BY source_id, device_id, metric
            ) latest ON latest.max_id = r.id
        """
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return {(r["source_id"], r["device_id"], r["metric"]): r["unit"] for r in rows}

    def latest_readings(self, source_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE source_id = ?" if source_id else ""
        args: list[Any] = [source_id] if source_id else []
        q = f"""
            SELECT r.source_id, r.device_id, r.metric, r.value, r.value_text, r.unit, r.ts
            FROM readings r
            JOIN (
                SELECT source_id, device_id, metric, MAX(id) AS max_id
                FROM readings
                {where}
                GROUP BY source_id, device_id, metric
            ) latest ON latest.max_id = r.id
            ORDER BY r.source_id, r.device_id, r.metric
        """
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # reporting config
    # ------------------------------------------------------------------ #
    def set_reporting_config(
        self,
        source_id: str,
        device_id: str,
        cluster_id: int,
        attr_id: int,
        min_interval: int,
        max_interval: int,
        reportable_change: int | None = None,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO reporting_config
                    (source_id, device_id, cluster_id, attr_id,
                     min_interval, max_interval, reportable_change, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id, device_id, cluster_id, attr_id) DO UPDATE SET
                    min_interval = excluded.min_interval,
                    max_interval = excluded.max_interval,
                    reportable_change = excluded.reportable_change,
                    updated_at = excluded.updated_at
                """,
                (source_id, device_id, cluster_id, attr_id,
                 min_interval, max_interval, reportable_change, now),
            )

    def get_reporting_config(
        self, source_id: str | None = None
    ) -> dict[tuple[str, str, int, int], dict[str, Any]]:
        """Map (source_id, device_id, cluster_id, attr_id) -> config dict."""
        q = "SELECT source_id, device_id, cluster_id, attr_id, min_interval, max_interval, reportable_change, updated_at FROM reporting_config"
        args: list[Any] = []
        if source_id:
            q += " WHERE source_id = ?"
            args.append(source_id)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return {
            (r["source_id"], r["device_id"], r["cluster_id"], r["attr_id"]): dict(r)
            for r in rows
        }
