from dataclasses import dataclass


@dataclass(slots=True)
class SpotifyTrack:
    source_id: str
    title: str
    artists: list[str]
    duration_ms: int | None = None
    album: str | None = None
    source_url: str | None = None
    score: float | None = None

    @property
    def artist_text(self) -> str:
        return ", ".join(self.artists)


@dataclass(slots=True)
class YandexTrack:
    track_id: str
    album_id: str
    title: str
    artists: list[str]
    duration_ms: int | None
    score: float | None = None
