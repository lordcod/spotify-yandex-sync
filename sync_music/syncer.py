from __future__ import annotations

import hashlib
import json

from .config import resolve_path
from .daily_mix import SpotifyWebDailyMix
from .db import Database
from .matcher import Matcher
from .spotify import SpotifyClient
from .yandex import YandexTarget, load_yandex_client


def source_hash(tracks) -> str:
    payload = [
        [t.source_id, t.title, t.artists, t.duration_ms]
        for t in tracks
    ]
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        self.matcher = Matcher(self.yandex_client, self.db, cfg["matching"])

    def close(self):
        self.db.close()

    def _source_tracks(self, binding: dict):
        source = binding["source"]
        if source == "spotify_liked":
            return self.spotify.liked_tracks(), "spotify"
        if source == "spotify_playlist":
            playlist_id = str(binding.get("source_id", "")).strip()
            if not playlist_id:
                raise ValueError(f"{binding['name']}: source_id пуст")
            return self.spotify.playlist_tracks(playlist_id), "spotify"
        if source == "spotify_daily_mix":
            url = str(binding.get("source_url", "")).strip()
            if not url:
                raise ValueError(f"{binding['name']}: source_url пуст")
            web = SpotifyWebDailyMix(resolve_path(self.cfg["spotify"]["browser_profile"]))
            return web.get_tracks(url, headless=True), "spotify"
        raise ValueError(f"Неизвестный source: {source}")

    @staticmethod
    def _target_kind(binding: dict) -> str:
        explicit = str(binding.get("target", "")).strip()
        if explicit:
            return explicit
        # Backward-compatible migration: old configs did not have `target`.
        # Liked Songs now naturally target native Yandex likes by default.
        if binding.get("source") == "spotify_liked":
            return "yandex_likes"
        return "yandex_playlist"

    def _sync_native_likes(self, name, binding, tracks, matched_pairs, digest):
        sync_mode = str(binding.get("sync_mode", "mirror")).strip().lower()
        if sync_mode not in {"mirror", "add_only"}:
            raise ValueError(
                f"{name}: sync_mode должен быть 'mirror' или 'add_only', получено {sync_mode!r}"
            )

        current_source_ids = {track.source_id for track in tracks}
        matched_by_source = {track.source_id: ref for track, ref in matched_pairs}
        previous = self.db.get_binding_tracks(name)

        # In mirror mode remove only likes previously managed by this binding
        # when their Spotify source track was removed, or when the mapping has
        # changed to a different Yandex track. Unrelated Yandex likes survive.
        managed_remove_ids: set[str] = set()
        if sync_mode == "mirror":
            for source_id, row in previous.items():
                old_yandex_id = str(row["yandex_track_id"])
                if source_id not in current_source_ids:
                    managed_remove_ids.add(old_yandex_id)
                    continue
                new_ref = matched_by_source.get(source_id)
                if new_ref is not None and str(new_ref.track_id) != old_yandex_id:
                    managed_remove_ids.add(old_yandex_id)

        desired_ids = {str(ref.track_id) for _, ref in matched_pairs}
        result = self.target.sync_likes(desired_ids, managed_remove_ids)

        # Preserve previous state for a currently-present source track if it
        # happens to be unmatched on this run. This prevents a transient
        # matcher failure from forgetting that we manage its old Yandex like.
        next_state: dict[str, tuple[str, str | None]] = {}
        for source_id, row in previous.items():
            if source_id in current_source_ids and source_id not in matched_by_source:
                next_state[source_id] = (
                    str(row["yandex_track_id"]),
                    str(row["yandex_album_id"]) if row["yandex_album_id"] is not None else None,
                )
        for source_id, ref in matched_by_source.items():
            next_state[source_id] = (str(ref.track_id), str(ref.album_id))

        self.db.replace_binding_tracks(
            name,
            [
                (source_id, yandex_id, album_id)
                for source_id, (yandex_id, album_id) in next_state.items()
            ],
        )
        self.db.save_state(
            name,
            digest,
            "likes",
            "Yandex Music — Мне нравится",
            len(tracks),
            len(matched_pairs),
        )

        return result

    def sync_binding(self, binding: dict) -> dict:
        name = binding["name"]
        run_id = self.db.start_run(name)
        try:
            tracks, source_kind = self._source_tracks(binding)
            digest = source_hash(tracks)
            print(f"[{name}] source: {len(tracks)} tracks")

            matched_pairs = []
            unmatched = []
            for i, track in enumerate(tracks, 1):
                ref = self.matcher.match(source_kind, track)
                if ref:
                    matched_pairs.append((track, ref))
                else:
                    unmatched.append(track)
                    print(f"[{name}] unmatched: {track.artist_text} — {track.title}")
                if i % 25 == 0:
                    print(f"[{name}] matched {len(matched_pairs)}/{i}")

            matched = [ref for _, ref in matched_pairs]
            target_kind = self._target_kind(binding)

            if target_kind == "yandex_likes":
                like_result = self._sync_native_likes(
                    name, binding, tracks, matched_pairs, digest
                )
                changed = bool(like_result["changed"])
                message = (
                    f"[{name}] {'updated' if changed else 'no changes'}: "
                    f"{len(matched)} matched, {len(unmatched)} unmatched, "
                    f"likes +{like_result['added']}/-{like_result['removed']}"
                )
            elif target_kind == "yandex_playlist":
                target_title = str(binding.get("target_title", "")).strip()
                if not target_title:
                    raise ValueError(f"{name}: для yandex_playlist нужен target_title")
                playlist = self.target.find_or_create_playlist(
                    target_title,
                    binding.get("target_visibility", "private"),
                )
                changed = self.target.replace_if_changed(playlist, matched)
                self.db.save_state(
                    name, digest, str(playlist.kind), target_title,
                    len(tracks), len(matched)
                )
                message = (
                    f"[{name}] {'updated' if changed else 'no changes'}: "
                    f"{len(matched)} matched, {len(unmatched)} unmatched"
                )
            else:
                raise ValueError(f"{name}: неизвестный target: {target_kind}")

            print(message)
            self.db.finish_run(
                run_id,
                source_count=len(tracks),
                matched_count=len(matched),
                unmatched_count=len(unmatched),
                changed=1 if changed else 0,
                status="ok",
                message=message,
            )
            return {
                "name": name,
                "source_count": len(tracks),
                "matched_count": len(matched),
                "unmatched_count": len(unmatched),
                "changed": changed,
            }
        except Exception as exc:
            self.db.finish_run(run_id, status="error", message=str(exc))
            raise

    def sync(self, name: str):
        bindings = self.cfg.get("playlists", [])
        if name == "all":
            selected = bindings
        else:
            selected = [b for b in bindings if b.get("name") == name]
        if not selected:
            raise ValueError(f"Не найден binding '{name}' в config.yaml")
        return [self.sync_binding(b) for b in selected]
