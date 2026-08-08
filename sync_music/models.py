from dataclasses import dataclass


@dataclass(slots=True)
class SourceTrack:
    source_id: str
    title: str
    artists: list[str]
    duration_ms: int | None = None
    album: str | None = None
    source_url: str | None = None

    @property
    def artist_text(self) -> str:
        return ", ".join(self.artists)


@dataclass(slots=True)
class YandexTrackRef:
    track_id: str
    album_id: str
    title: str
    artists: list[str]
    duration_ms: int | None
    score: float

    @property
    def full_id(self) -> str:
        return f"{self.track_id}:{self.album_id}"
