from __future__ import annotations

from .config import resolve_path
from .db import Database
from .matcher import Matcher
from .likes import sync_likes
from .spotify import SpotifyClient
from .yandex import YandexTarget, load_yandex_client


class SyncEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        spotify_cfg = cfg["spotify"]
        self.spotify = SpotifyClient(
            client_id=spotify_cfg.get("client_id", ""),
            redirect_uri=spotify_cfg["redirect_uri"],
            token_file=resolve_path(spotify_cfg["token_file"]),
            timeout=int(spotify_cfg.get("request_timeout_seconds", 30)),
        )
        self.yandex_client = load_yandex_client(resolve_path(cfg["yandex"]["token_file"]))
        self.target = YandexTarget(self.yandex_client)
        self.db = Database(resolve_path("data/sync.db"))
        self.matcher = Matcher(self.yandex_client, self.spotify, self.db, cfg["matching"])

    def close(self):
        self.db.close()

    def sync_binding(self, binding: dict) -> dict:
        if binding.get("source") != "likes":
            raise ValueError("Only bidirectional likes sync is supported (source: likes)")
        run_id = self.db.start_run(binding["name"])
        try:
            result = sync_likes(self, binding)
            self.db.finish_run(run_id, status="ok", **{k: v for k, v in result.items() if k != "name"})
            return result
        except Exception as exc:
            self.db.finish_run(run_id, status="error", message=str(exc))
            raise

    def sync(self, name: str = "all"):
        bindings = self.cfg["playlists"]
        selected = [b for b in bindings if name == "all" or b["name"] == name]
        if not selected:
            raise ValueError(f"Unknown sync job: {name}")
        return [self.sync_binding(b) for b in selected]
