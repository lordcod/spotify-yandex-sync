from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .models import SourceTrack


TRACK_ID_RE = re.compile(r"/track/([A-Za-z0-9]+)")


def _normalize_spotify_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc not in {"open.spotify.com", "spotify.link"}:
        return url
    return url.split("?", 1)[0]


class SpotifyWebDailyMix:
    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    def setup_login(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), headless=False
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto("https://open.spotify.com/", wait_until="domcontentloaded")
            print("В открывшемся Chromium войди в Spotify.")
            input("Когда увидишь свой Spotify Web Player, нажми Enter здесь... ")
            browser.close()
        print("Browser-сессия Spotify сохранена.")

    def get_tracks(self, playlist_url: str, headless: bool = True) -> list[SourceTrack]:
        from playwright.sync_api import sync_playwright

        url = _normalize_spotify_url(playlist_url)
        result: list[SourceTrack] = []
        seen: set[str] = set()

        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=headless,
                viewport={"width": 1440, "height": 1000},
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3500)

            # Spotify virtualizes long lists, so scroll and collect rows repeatedly.
            stable_rounds = 0
            last_count = 0
            for _ in range(30):
                rows = page.locator('[data-testid="tracklist-row"]')
                for i in range(rows.count()):
                    row = rows.nth(i)
                    track_link = row.locator('a[href*="/track/"]').first
                    if track_link.count() == 0:
                        continue
                    href = track_link.get_attribute("href") or ""
                    m = TRACK_ID_RE.search(href)
                    if not m:
                        continue
                    track_id = m.group(1)
                    if track_id in seen:
                        continue
                    title = (track_link.inner_text() or "").strip()
                    artist_links = row.locator('a[href*="/artist/"]')
                    artists = []
                    for j in range(artist_links.count()):
                        name = (artist_links.nth(j).inner_text() or "").strip()
                        if name and name not in artists:
                            artists.append(name)
                    if title:
                        seen.add(track_id)
                        result.append(
                            SourceTrack(
                                source_id=track_id,
                                title=title,
                                artists=artists,
                                source_url=f"https://open.spotify.com/track/{track_id}",
                            )
                        )

                page.mouse.wheel(0, 1800)
                page.wait_for_timeout(500)
                if len(result) == last_count:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_count = len(result)
                if stable_rounds >= 4:
                    break

            browser.close()

        if not result:
            raise RuntimeError(
                "Не удалось прочитать треки Daily Mix. Выполни 'python run.py auth spotify-web' "
                "и проверь source_url. Spotify Web может поменять разметку — см. README."
            )
        return result
