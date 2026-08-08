from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS track_map (
    source_kind TEXT NOT NULL,
    source_track_id TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_artists TEXT NOT NULL,
    source_duration_ms INTEGER,
    yandex_track_id TEXT,
    yandex_album_id TEXT,
    yandex_title TEXT,
    yandex_artists TEXT,
    match_score REAL,
    status TEXT NOT NULL DEFAULT 'matched',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source_kind, source_track_id)
);

CREATE TABLE IF NOT EXISTS playlist_state (
    binding_name TEXT PRIMARY KEY,
    source_hash TEXT,
    yandex_playlist_kind TEXT,
    target_title TEXT,
    last_synced_at TEXT,
    last_source_count INTEGER,
    last_matched_count INTEGER
);

-- Tracks managed by a particular sync binding. This is especially important
-- for Spotify Liked Songs -> native Yandex likes: it lets us remove a Yandex
-- like only after that Spotify track was previously managed by this binding,
-- without touching unrelated likes that existed in Yandex beforehand.
CREATE TABLE IF NOT EXISTS binding_track_state (
    binding_name TEXT NOT NULL,
    source_track_id TEXT NOT NULL,
    yandex_track_id TEXT NOT NULL,
    yandex_album_id TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (binding_name, source_track_id)
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

    def get_mapping(self, source_kind: str, source_track_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM track_map WHERE source_kind=? AND source_track_id=?",
            (source_kind, source_track_id),
        ).fetchone()

    def save_mapping(
        self,
        source_kind: str,
        source_track_id: str,
        source_title: str,
        source_artists: str,
        source_duration_ms: int | None,
        yandex_track_id: str | None,
        yandex_album_id: str | None,
        yandex_title: str | None,
        yandex_artists: str | None,
        score: float | None,
        status: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO track_map (
                source_kind, source_track_id, source_title, source_artists,
                source_duration_ms, yandex_track_id, yandex_album_id,
                yandex_title, yandex_artists, match_score, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_kind, source_track_id) DO UPDATE SET
                source_title=excluded.source_title,
                source_artists=excluded.source_artists,
                source_duration_ms=excluded.source_duration_ms,
                yandex_track_id=excluded.yandex_track_id,
                yandex_album_id=excluded.yandex_album_id,
                yandex_title=excluded.yandex_title,
                yandex_artists=excluded.yandex_artists,
                match_score=excluded.match_score,
                status=excluded.status,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                source_kind, source_track_id, source_title, source_artists,
                source_duration_ms, yandex_track_id, yandex_album_id,
                yandex_title, yandex_artists, score, status,
            ),
        )
        self.conn.commit()

    def get_binding_tracks(self, binding_name: str) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM binding_track_state WHERE binding_name=?",
            (binding_name,),
        ).fetchall()
        return {str(row["source_track_id"]): row for row in rows}

    def replace_binding_tracks(
        self,
        binding_name: str,
        rows: Iterable[tuple[str, str, str | None]],
    ) -> None:
        """Atomically replace the managed source->Yandex mapping for a binding."""
        rows = list(rows)
        with self.conn:
            self.conn.execute(
                "DELETE FROM binding_track_state WHERE binding_name=?",
                (binding_name,),
            )
            self.conn.executemany(
                """
                INSERT INTO binding_track_state (
                    binding_name, source_track_id, yandex_track_id,
                    yandex_album_id, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                [
                    (binding_name, source_id, yandex_id, album_id)
                    for source_id, yandex_id, album_id in rows
                ],
            )

    def get_state(self, binding_name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM playlist_state WHERE binding_name=?", (binding_name,)
        ).fetchone()

    def save_state(
        self,
        binding_name: str,
        source_hash: str,
        yandex_playlist_kind: str,
        target_title: str,
        source_count: int,
        matched_count: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO playlist_state (
                binding_name, source_hash, yandex_playlist_kind, target_title,
                last_synced_at, last_source_count, last_matched_count
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(binding_name) DO UPDATE SET
                source_hash=excluded.source_hash,
                yandex_playlist_kind=excluded.yandex_playlist_kind,
                target_title=excluded.target_title,
                last_synced_at=CURRENT_TIMESTAMP,
                last_source_count=excluded.last_source_count,
                last_matched_count=excluded.last_matched_count
            """,
            (
                binding_name, source_hash, yandex_playlist_kind, target_title,
                source_count, matched_count,
            ),
        )
        self.conn.commit()

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
