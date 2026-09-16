from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .models import SpotifyTrack


AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
SCOPES = "user-library-read user-library-modify playlist-read-private"

class SpotifyAuthError(RuntimeError):
    pass


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        type(self).result = {k: v[0] for k, v in params.items() if v}
        body = "Spotify authorization received. You can close this tab."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args):
        return


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        token_file: Path,
        timeout: int = 30,
    ):
        self.client_id = client_id.strip()
        self.redirect_uri = redirect_uri
        self.token_file = token_file
        self.timeout = timeout
        self.session = requests.Session()
        if not self.client_id or "PASTE_" in self.client_id:
            raise SpotifyAuthError("Укажи spotify.client_id в config.yaml")

    def _save_token(self, token: dict) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        token["obtained_at"] = int(time.time())
        self.token_file.write_text(json.dumps(token, indent=2), encoding="utf-8")

    def _load_token(self) -> dict:
        if not self.token_file.exists():
            raise SpotifyAuthError("Spotify не авторизован. Выполни: python run.py auth spotify")
        return json.loads(self.token_file.read_text(encoding="utf-8"))

    def authorize_pkce(self) -> None:
        parsed = urlparse(self.redirect_uri)
        if parsed.hostname not in {"127.0.0.1", "::1"}:
            raise SpotifyAuthError(
                "Для локального запуска redirect_uri должен использовать 127.0.0.1 или ::1"
            )
        port = parsed.port or 80
        path = parsed.path or "/"

        verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        state = secrets.token_urlsafe(20)

        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
        auth_url = f"{AUTH_URL}?{urlencode(params)}"

        _CallbackHandler.result = {}
        server = HTTPServer((parsed.hostname, port), _CallbackHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        print("Открываю Spotify для авторизации:")
        print(auth_url)
        webbrowser.open(auth_url)
        thread.join(timeout=180)
        server.server_close()

        result = _CallbackHandler.result
        if not result:
            raise SpotifyAuthError("Не дождался callback от Spotify")
        if result.get("state") != state:
            raise SpotifyAuthError("Spotify OAuth state mismatch")
        if "error" in result:
            raise SpotifyAuthError(f"Spotify OAuth error: {result['error']}")
        code = result.get("code")
        if not code:
            raise SpotifyAuthError("Spotify не вернул authorization code")

        resp = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "code_verifier": verifier,
            },
            timeout=self.timeout,
        )
        self._raise(resp)
        self._save_token(resp.json())
        print("Spotify авторизован.")

    def _access_token(self) -> str:
        token = self._load_token()
        expires_in = int(token.get("expires_in", 3600))
        obtained_at = int(token.get("obtained_at", 0))
        if time.time() >= obtained_at + expires_in - 60:
            refresh = token.get("refresh_token")
            if not refresh:
                raise SpotifyAuthError("Нет refresh_token. Выполни auth spotify заново.")
            resp = self.session.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
                timeout=self.timeout,
            )
            self._raise(resp)
            new_token = resp.json()
            new_token.setdefault("refresh_token", refresh)
            self._save_token(new_token)
            token = new_token
        return token["access_token"]

    def _raise(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        detail = resp.text[:1000]
        raise SpotifyAuthError(f"Spotify HTTP {resp.status_code}: {detail}")

    def get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            resp = self.session.get(
                f"{API_URL}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}"},
                timeout=self.timeout,
            )
            if resp.status_code == 429 and attempt < 3:
                print('Spotify rate limit hit. Retrying in %s seconds...' % resp.headers.get("Retry-After", "2"))
                time.sleep(int(resp.headers.get("Retry-After", "2")))
                continue
            self._raise(resp)
            return resp.json()
        raise SpotifyAuthError("Spotify rate limit")

    def search(self, query: str) -> list[SpotifyTrack]:
        results = self.get(
            "/search",
            {
                "q": query,
                "type": "track",
                "include_external": "audio",
                "locale": "ru",
            },
        )

        tracks = results.get("tracks", {}).get("items", [])

        return [
            parsed
            for track in tracks
            if (parsed := self._to_track(track)) is not None
        ]

    @staticmethod
    def _to_track(obj: dict) -> SpotifyTrack | None:
        if not obj or obj.get("type") not in {None, "track"}:
            return None

        track_id = obj.get("id")
        title = obj.get("name")

        if not track_id or not title:
            return None

        artists = [
            a["name"]
            for a in obj.get("artists", [])
            if a.get("name")
        ]


        track = SpotifyTrack(
            source_id=str(track_id),
            title=title,
            artists=artists,
            duration_ms=obj.get("duration_ms"),
            album=(obj.get("album") or {}).get("name"),
            source_url=(obj.get("external_urls") or {}).get("spotify"),
        )

        return track

    def liked_tracks(self) -> list[SpotifyTrack]:
        items: list[SpotifyTrack] = []
        offset = 0
        while True:
            data = self.get("/me/tracks", {"limit": 50, "offset": offset})
            batch = data.get("items", [])
            for wrapper in batch:
                track = self._to_track(wrapper.get("track") or wrapper.get("item") or {})
                if track:
                    items.append(track)
            if not data.get("next") or not batch:
                break
            offset += len(batch)
        return items

    def current_user_playlists(self) -> list[dict]:
        playlists: list[dict] = []
        offset = 0
        while True:
            data = self.get("/me/playlists", {"limit": 50, "offset": offset})
            batch = data.get("items", [])
            playlists.extend(batch)
            if not data.get("next") or not batch:
                break
            offset += len(batch)
        return playlists

    def playlist_tracks(self, playlist_id: str) -> list[SpotifyTrack]:
        items: list[SpotifyTrack] = []
        offset = 0
        while True:
            data = self.get(
                f"/playlists/{playlist_id}/items",
                {"limit": 50, "offset": offset},
            )
            batch = data.get("items", [])
            for wrapper in batch:
                track = self._to_track(wrapper.get("item") or wrapper.get("track") or {})
                if track:
                    items.append(track)
            if not data.get("next") or not batch:
                break
            offset += len(batch)
        return items

    def _library_request(self, method: str, track_ids: set[str]) -> None:
        ids = list(track_ids)

        for start in range(0, len(ids), 40):
            batch = ids[start:start + 40]

            uris = ",".join(
                f"spotify:track:{track_id}"
                for track_id in batch
            )

            for attempt in range(4):
                resp = self.session.request(
                    method,
                    f"{API_URL}/me/library",
                    params={"uris": uris},
                    headers={
                        "Authorization": f"Bearer {self._access_token()}"
                    },
                    timeout=self.timeout,
                )

                if resp.status_code == 429 and attempt < 3:
                    time.sleep(int(resp.headers.get("Retry-After", "2")))
                    continue

                self._raise(resp)
                break
    
    def sync_likes(
        self,
        desired_track_ids: set[str],
        managed_remove_ids: set[str] | None = None,
    ) -> dict[str, int | bool]:
        current = {
            track.source_id
            for track in self.liked_tracks()
        }

        to_add = desired_track_ids - current

        to_remove = (
            set()
            if managed_remove_ids is None
            else (current & managed_remove_ids) - desired_track_ids
        )

        if to_add:
            print('Add', to_add)
            # self._library_request("PUT", to_add)

        if to_remove:
            print('Remove', to_remove)
            # self._library_request("DELETE", to_remove)

        return {
            "changed": bool(to_add or to_remove),
            "added": len(to_add),
            "removed": len(to_remove),
            "current_before": len(current),
            "current_after": len(current) + len(to_add) - len(to_remove),
        }
