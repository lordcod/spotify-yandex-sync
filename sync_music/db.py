from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS track_map (
    source_kind TEXT NOT NULL,
    source_track_id TEXT,
    source_title TEXT,
    source_artists TEXT,
    source_duration_ms INTEGER,
    yandex_track_id TEXT,
    yandex_album_id TEXT,
    yandex_title TEXT,
    yandex_artists TEXT,
    match_score REAL,
    status TEXT NOT NULL DEFAULT 'matched',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_kind, source_track_id, yandex_track_id)
);

CREATE TABLE IF NOT EXISTS likes_ignored (
    name TEXT PRIMARY KEY,
    spotify TEXT NOT NULL,
    yandex TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS likes_sync_state (
    name TEXT PRIMARY KEY,
    pairs TEXT NOT NULL DEFAULT '[]',
    pending TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    binding_name TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    source_count INTEGER DEFAULT 0,
    matched_count INTEGER DEFAULT 0,
    unmatched_count INTEGER DEFAULT 0,
    changed INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_mapping(self, source_track_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM track_map WHERE source_track_id=? ORDER BY (status='matched') DESC, updated_at DESC",
            (source_track_id, ),
        ).fetchone()
        
    def get_mapping_from_yandex(self, yandex_track_id: str, yandex_album_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM track_map WHERE yandex_track_id=? AND yandex_album_id=? ORDER BY (status='matched') DESC, updated_at DESC",
            (yandex_track_id, yandex_album_id),
        ).fetchone()

    def save_mapping(
        self,
        source_kind: str | None,
        source_track_id: str | None,
        source_title: str | None,
        source_artists: str | None,
        source_duration_ms: int | None,
        yandex_track_id: str | None,
        yandex_album_id: str | None,
        yandex_title: str | None,
        yandex_artists: str | None,
        score: float | None,
        status: str,
    ) -> None:
        # Explicit replacement supports both historical primary-key layouts,
        # and NULL Spotify IDs for unmatched Yandex tracks.
        with self.conn:
            self.conn.execute(
                "DELETE FROM track_map WHERE source_kind=? AND "
                + ("source_track_id=?" if source_track_id is not None else
                   "source_track_id IS NULL AND yandex_track_id=?"),
                (source_kind, source_track_id if source_track_id is not None else yandex_track_id),
            )
            self.conn.execute(
                """INSERT INTO track_map (
                    source_kind, source_track_id, source_title, source_artists,
                    source_duration_ms, yandex_track_id, yandex_album_id,
                    yandex_title, yandex_artists, match_score, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_kind, source_track_id, source_title, source_artists,
                 source_duration_ms, yandex_track_id, yandex_album_id,
                 yandex_title, yandex_artists, score, status),
            )

    def start_run(self, binding_name: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sync_runs(binding_name) VALUES (?)", (binding_name,)
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, **fields: Any) -> None:
        allowed = {
            "source_count", "matched_count", "unmatched_count", "changed",
            "status", "message"
        }
        parts, values = [], []
        for key, value in fields.items():
            if key in allowed:
                parts.append(f"{key}=?")
                values.append(value)
        parts.append("finished_at=CURRENT_TIMESTAMP")
        values.append(run_id)
        self.conn.execute(
            f"UPDATE sync_runs SET {', '.join(parts)} WHERE id=?", values
        )
        self.conn.commit()

    def likes_state(self, name: str) -> tuple[list, dict | None]:
        row = self.conn.execute("SELECT pairs, pending FROM likes_sync_state WHERE name=?", (name,)).fetchone()
        return (json.loads(row["pairs"]), json.loads(row["pending"]) if row["pending"] else None) if row else ([], None)

    def save_likes_state(self, name: str, pairs: list, pending: dict | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO likes_sync_state(name, pairs, pending) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET pairs=excluded.pairs, pending=excluded.pending",
                (name, json.dumps(pairs), json.dumps(pending) if pending is not None else None),
            )

    def ignored_likes(self, name: str) -> tuple[set[str], set[str]] | None:
        row = self.conn.execute("SELECT spotify, yandex FROM likes_ignored WHERE name=?", (name,)).fetchone()
        return (set(json.loads(row[0])), set(json.loads(row[1]))) if row else None

    def save_ignored_likes(self, name: str, spotify: set[str], yandex: set[str]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO likes_ignored VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "spotify=excluded.spotify, yandex=excluded.yandex",
                (name, json.dumps(sorted(spotify)), json.dumps(sorted(yandex))),
            )
