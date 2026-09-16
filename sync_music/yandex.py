from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterable

from yandex_music import Client, Track
from yandex_music.utils.difference import Difference

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

    # ---------- Regular Yandex playlists ----------

    def find_or_create_playlist(self, title: str, visibility: str = "private"):
        playlists = self.client.users_playlists_list()
        exact = [p for p in playlists if getattr(p, "title", None) == title]
        if exact:
            # Fetch the full playlist, including tracks/revision.
            return self.client.users_playlists(exact[0].kind)
        created = self.client.users_playlists_create(title, visibility=visibility)
        if created is None:
            raise RuntimeError(f"Yandex не создал плейлист: {title}")
        return self.client.users_playlists(created.kind)

    @staticmethod
    def playlist_ids(playlist) -> list[str]:
        ids: list[str] = []
        for item in getattr(playlist, "tracks", None) or []:
            track_id = getattr(item, "id", None)
            album_id = getattr(item, "album_id", None)
            if track_id and album_id:
                ids.append(f"{track_id}:{album_id}")
        return ids

    def replace_if_changed(self, playlist, desired: list[YandexTrack]) -> bool:
        current_ids = self.playlist_ids(playlist)
        desired_ids = [x.full_id for x in desired]
        if current_ids == desired_ids:
            return False

        diff = Difference()
        if current_ids:
            # Yandex difference uses a half-open range [from, to).
            diff.add_delete(0, len(current_ids))
        if desired:
            diff.add_insert(
                0,
                [{"id": x.track_id, "album_id": x.album_id} for x in desired],
            )
        changed = self.client.users_playlists_change(
            playlist.kind,
            diff.to_json(),
            revision=playlist.revision,
            user_id=playlist.owner.uid,
        )
        if changed is None:
            raise RuntimeError("Yandex вернул пустой ответ при изменении плейлиста")
        return True

    # ---------- Native Yandex Music likes / «Мне нравится» ----------

    def liked_track_ids(self) -> set[str]:
        """Return Yandex track IDs currently liked by the authenticated user."""
        liked = self.client.users_likes_tracks()
        if liked is None:
            return set()

        # TracksList is iterable, but keeping this fallback makes the code
        # tolerant to older/newer library model implementations.
        items = getattr(liked, "tracks", None)
        if items is None:
            items = liked

        result: set[str] = set()
        for item in items or []:
            track_id = getattr(item, "id", None)
            if track_id is not None:
                result.add(str(track_id))
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
        to_remove = sorted((managed_remove_ids or set()) & current)

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
        return [
            YandexTrack(track.id,
                        track.albums[0].id, 
                        track.title, 
                        [art.name for art in track.artists],
                        track.duration_ms)
            for track in self.client.tracks(list(self.liked_track_ids()))
            if track.albums
        ]
