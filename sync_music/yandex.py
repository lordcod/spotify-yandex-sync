from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from yandex_music import Client

from .models import YandexTrack


class YandexAuthError(RuntimeError):
    pass


def authorize_yandex(token_file: Path) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)

    def on_code(code):
        print(f"Открой: {code.verification_url}")
        print(f"Введи код: {code.user_code}")

    client = Client()
    token = client.device_auth(on_code=on_code)
    payload = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_in": token.expires_in,
        "token_type": token.token_type,
        "obtained_at": int(time.time()),
    }
    token_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Yandex Music авторизован.")


def load_yandex_client(token_file: Path) -> Client:
    if not token_file.exists():
        raise YandexAuthError("Yandex не авторизован. Выполни: python run.py auth yandex")
    payload = json.loads(token_file.read_text(encoding="utf-8"))
    token = payload.get("access_token")
    if not token:
        raise YandexAuthError("В yandex token file нет access_token")
    try:
        return Client(token).init()
    except Exception as exc:
        raise YandexAuthError(
            "Не удалось авторизоваться в Yandex Music. Возможно, access_token истёк. "
            "Повтори: python run.py auth yandex"
        ) from exc


def _chunks(values: list[str], size: int = 100) -> Iterable[list[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


class YandexTarget:
    def __init__(self, client: Client):
        self.client = client

    def liked_track_ids(self) -> set[str]:
        return set(self.ordered_liked_track_ids())

    def ordered_liked_track_ids(self) -> list[str]:
        """Return Yandex track IDs currently liked by the authenticated user."""
        liked = self.client.users_likes_tracks()
        if liked is None:
            raise RuntimeError("Yandex returned no likes snapshot")

        # TracksList is iterable, but keeping this fallback makes the code
        # tolerant to older/newer library model implementations.
        items = getattr(liked, "tracks", None)
        if items is None:
            items = liked

        items = list(items or [])
        # Use like timestamps when available; otherwise retain API order.
        if items and all(getattr(item, "timestamp", None) for item in items):
            items.sort(key=lambda item: datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")).timestamp(), reverse=True)
        result: list[str] = []
        for item in items:
            track_id = getattr(item, "id", None)
            if track_id is not None:
                result.append(str(track_id))
        return result

    def sync_likes(
        self,
        desired_track_ids: set[str],
        managed_remove_ids: set[str] | None = None,
    ) -> dict[str, int | bool]:
        """
        Synchronize native Yandex likes.

        desired_track_ids are always ensured to be liked.
        managed_remove_ids are removed only if they are currently liked. The
        caller supplies only IDs previously managed by this sync binding, so
        unrelated pre-existing Yandex likes are preserved.
        """
        current = self.liked_track_ids()
        to_add = sorted(desired_track_ids - current)
        to_remove = sorted(((managed_remove_ids or set()) & current) - desired_track_ids)

        # Remove first so a changed Spotify->Yandex mapping cannot leave both
        # old and new versions liked after a successful run.
        for batch in _chunks(to_remove):
            ok = self.client.users_likes_tracks_remove(batch)
            if not ok:
                raise RuntimeError(
                    f"Yandex не подтвердил снятие лайков ({len(batch)} треков)"
                )

        for batch in _chunks(to_add):
            ok = self.client.users_likes_tracks_add(batch)
            if not ok:
                raise RuntimeError(
                    f"Yandex не подтвердил добавление лайков ({len(batch)} треков)"
                )

        return {
            "changed": bool(to_add or to_remove),
            "added": len(to_add),
            "removed": len(to_remove),
            "current_before": len(current),
        }
    
    def get_likes(self) -> list[YandexTrack]:
        result = []
        ids = self.ordered_liked_track_ids()
        for batch in _chunks(ids):
            tracks = self.client.tracks(batch)
            if tracks is None or {str(t.id) for t in tracks} != set(batch):
                raise RuntimeError("Yandex returned an incomplete likes snapshot")
            by_id = {str(track.id): track for track in tracks}
            for track in (by_id[track_id] for track_id in batch):
                result.append(YandexTrack(
                    str(track.id), str(track.albums[0].id) if track.albums else "",
                    track.title or "", [art.name for art in track.artists], track.duration_ms,
                ))
        return result
