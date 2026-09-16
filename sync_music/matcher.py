from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass

from rapidfuzz import fuzz

from .db import Database
from .models import SpotifyTrack, YandexTrack


BRACKET_NOISE = re.compile(
    r"\s*[\(\[](?:remaster(?:ed)?(?:\s*\d{2,4})?|radio\s+edit|edit|version|"
    r"deluxe|bonus|explicit|clean|mono|stereo|original\s+mix|single\s+version)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
FEAT = re.compile(r"\s+(?:feat\.?|ft\.?)\s+.*$", re.IGNORECASE)
NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
SPACES = re.compile(r"\s+")

RU_TO_LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


@dataclass(slots=True)
class CandidateScore:
    total: float
    title: float
    artist: float
    duration: float
    album: float


class TextMatcher:
    """Text cleanup and fuzzy comparison for Spotify/Yandex metadata."""

    @staticmethod
    def normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
        text = BRACKET_NOISE.sub("", text)
        text = FEAT.sub("", text)
        text = NON_WORD.sub(" ", text)
        return SPACES.sub(" ", text).strip()

    @staticmethod
    def transliterate(text: str) -> str:
        text = unicodedata.normalize("NFKC", text or "").casefold()
        return "".join(RU_TO_LAT.get(char, char) for char in text)

    @classmethod
    def latinize(cls, text: str) -> str:
        return cls.normalize(cls.transliterate(text))

    @classmethod
    def similarity(cls, left: str, right: str) -> float:
        left_variants = {cls.normalize(left), cls.latinize(left)} - {""}
        right_variants = {cls.normalize(right), cls.latinize(right)} - {""}
        return max(
            (
                max(float(fuzz.ratio(a, b)), float(fuzz.token_set_ratio(a, b)))
                for a in left_variants
                for b in right_variants
            ),
            default=0.0,
        )

    @classmethod
    def artists_similarity(cls, source: list[str], target: list[str]) -> float:
        if not source:
            return 70.0
        if not target:
            return 0.0

        source_coverage = sum(max(cls.similarity(a, b) for b in target) for a in source) / len(source)
        target_coverage = sum(max(cls.similarity(b, a) for a in source) for b in target) / len(target)
        return source_coverage * 0.88 + target_coverage * 0.12


class CandidateScorer:
    """Scores a search result without knowing where it came from."""

    def __init__(self, duration_tolerance_ms: int):
        self.duration_tolerance_ms = duration_tolerance_ms

    def score(self, source: SpotifyTrack | YandexTrack, candidate) -> CandidateScore:
        title = TextMatcher.similarity(source.title, getattr(candidate, "title", "") or "")
        artist = TextMatcher.artists_similarity(source.artists, _artists(candidate))
        duration = self._duration(source.duration_ms, getattr(candidate, "duration_ms", None))
        album = self._album(source, candidate)

        total = title * 0.45 + artist * 0.32 + duration * 0.20 + album * 0.03
        if title < 70:
            total -= (70 - title) * 0.35
        if artist < 70:
            total -= (55 - artist) * 0.35

        return CandidateScore(max(0.0, total), title, artist, duration, album)

    def _duration(self, source_ms: int | None, target_ms: int | None) -> float:
        if not source_ms or not target_ms:
            return 70.0

        delta = abs(int(source_ms) - int(target_ms))
        if delta <= 1500:
            return 100.0
        if delta <= 3000:
            return 97.0
        if delta <= self.duration_tolerance_ms:
            span = max(1, self.duration_tolerance_ms - 3000)
            return 97.0 - ((delta - 3000) / span) * 27.0
        if delta <= 15000:
            return 45.0
        if delta <= 30000:
            return 20.0
        return 5.0

    @staticmethod
    def _album(source: SpotifyTrack | YandexTrack, candidate) -> float:
        if not isinstance(source, SpotifyTrack) or not source.album:
            return 70.0

        albums = [a.title for a in (getattr(candidate, "albums", None) or []) if getattr(a, "title", None)]
        return max((TextMatcher.similarity(source.album, title) for title in albums), default=70.0)


# Compatibility with existing imports from this module.
def normalize(text: str) -> str:
    return TextMatcher.normalize(text)


def transliterate_ru(text: str) -> str:
    return TextMatcher.transliterate(text)


def latinize(text: str) -> str:
    return TextMatcher.latinize(text)


def score_candidate(source, candidate, duration_tolerance_ms: int) -> CandidateScore:
    return CandidateScorer(duration_tolerance_ms).score(source, candidate)


def _artists(track) -> list[str]:
    try:
        return [name for name in track.artists_name() if name]
    except Exception:
        return [a for a in (getattr(track, "artists", None) or [])]


class Matcher:
    def __init__(self, yandex_client, spotify_client, db: Database, cfg: dict):
        self.client = yandex_client
        self.spotify_client = spotify_client
        self.db = db
        self.min_score = float(cfg.get("min_score", 86))
        self.high_confidence_score = float(cfg.get("high_confidence_score", 94))
        self.candidate_limit = int(cfg.get("candidate_limit", 30))
        self.debug_unmatched = bool(cfg.get("debug_unmatched", True))
        self.scorer = CandidateScorer(int(cfg.get("duration_tolerance_ms", 8000)))

    # Search -----------------------------------------------------------------

    def _search_queries(self, source: SpotifyTrack | YandexTrack) -> list[str]:
        artists = [a.strip() for a in source.artists[:2] if a and a.strip()]
        main_artist = artists[0] if artists else ""
        combined = " ".join([*artists, source.title])

        queries = [combined, f"{source.title} {main_artist}", source.title]
        latin = latinize(combined)
        if latin and latin != normalize(combined):
            queries.append(latin)

        result, seen = [], set()
        for query in queries:
            query = query.strip()
            key = normalize(query)
            if query and key and key not in seen:
                result.append(query)
                seen.add(key)
        return result

    def _search_yandex(self, query: str) -> list:
        try:
            page = self.client.search(query, type_="track")
            return list(getattr(getattr(page, "tracks", None), "results", None) or [])[: self.candidate_limit]
        except Exception:
            return []

    def _search_spotify(self, query: str) -> list:
        try:
            return list(self.spotify_client.search(query) or [])[: self.candidate_limit]
        except Exception:
            return []

    def _find_best_candidate(
        self,
        source: SpotifyTrack | YandexTrack,
        search: Callable[[str], list],
        key: Callable[[object], Hashable | None],
    ) -> tuple[object | None, CandidateScore | None]:
        candidates: dict[Hashable, object] = {}
        best = score = None

        for query in self._search_queries(source):
            for candidate in search(query):
                if (candidate_key := key(candidate)) is not None:
                    candidates.setdefault(candidate_key, candidate)

            best, score = self._best(source, candidates.values())
            if score and score.total >= self.high_confidence_score:
                break

        return best, score

    def _best(self, source, candidates: Iterable) -> tuple[object | None, CandidateScore | None]:
        best = best_score = None
        for candidate in candidates:
            score = self.scorer.score(source, candidate)
            if best_score is None or score.total > best_score.total:
                best, best_score = candidate, score
        return best, best_score

    @staticmethod
    def _yandex_key(track) -> tuple[str, str] | None:
        track_id = str(getattr(track, "id", "") or "")
        if not track_id:
            return None
        albums = getattr(track, "albums", None) or []
        album_id = str(getattr(albums[0], "id", "") or "") if albums else ""
        return track_id, album_id

    @staticmethod
    def _spotify_key(track) -> str | None:
        return getattr(track, "source_id", None) or None

    # Cache / DB --------------------------------------------------------------

    def _cached_yandex(self, source: SpotifyTrack) -> YandexTrack | None:
        row = self.db.get_mapping(source.source_id)
        if not row or row["status"] != "matched" or not row["yandex_track_id"]:
            return None
        return YandexTrack(
            row["yandex_track_id"], row["yandex_album_id"], row["yandex_title"] or source.title,
            (row["yandex_artists"] or "").split(" | ") if row["yandex_artists"] else [],
            None, float(row["match_score"] or 100),
        )

    def _cached_spotify(self, track: YandexTrack) -> SpotifyTrack | None:
        row = self.db.get_mapping_from_yandex(track.track_id, track.album_id)
        if not row or row["status"] != "matched" or not row["source_track_id"]:
            return None
        return SpotifyTrack(
            row["source_track_id"], row["source_title"], row["source_artists"],
            row["source_duration_ms"], "", "", row["match_score"],
        )

    def _save_unmatched(self, source_kind: str, source, score: CandidateScore | None) -> None:
        value = score.total if score else None
        if isinstance(source, SpotifyTrack):
            self.db.save_mapping(
                source_kind, source.source_id, source.title, source.artist_text, source.duration_ms,
                None, None, None, None, value, "unmatched",
            )
        else:
            self.db.save_mapping(
                source_kind, None, None, None, None,
                source.track_id, source.album_id, source.title, "".join(source.artists), value, "unmatched",
            )

    def _save_match(self, source_kind: str, source, target, score: CandidateScore) -> None:
        if isinstance(source, SpotifyTrack):
            self.db.save_mapping(
                source_kind, source.source_id, source.title, source.artist_text, source.duration_ms,
                target.track_id, target.album_id, target.title, " | ".join(target.artists),
                score.total, "matched",
            )
        else:
            self.db.save_mapping(
                source_kind, target.source_id, target.title, target.artist_text, target.duration_ms,
                source.track_id, source.album_id, source.title, ", ".join(source.artists),
                score.total, "matched",
            )

    def _debug_rejected(self, candidate, score: CandidateScore | None) -> None:
        if not self.debug_unmatched or candidate is None or score is None:
            return
        print(
            f"[matcher] best rejected {score.total:.1f} "
            f"(title={score.title:.0f}, artist={score.artist:.0f}, duration={score.duration:.0f}): "
            f"{', '.join(_artists(candidate))} — {getattr(candidate, 'title', '?')}"
        )

    def _accepted(self, candidate, score: CandidateScore | None) -> bool:
        return candidate is not None and score is not None and score.total >= self.min_score

    # Public API --------------------------------------------------------------

    def match(self, source_kind: str, source: SpotifyTrack) -> YandexTrack | None:
        if cached := self._cached_yandex(source):
            return cached

        candidate, score = self._find_best_candidate(source, self._search_yandex, self._yandex_key)
        if not self._accepted(candidate, score):
            self._save_unmatched(source_kind, source, score)
            self._debug_rejected(candidate, score)
            return None

        albums = getattr(candidate, "albums", None) or []
        album_id = str(albums[0].id) if albums else ""
        if not album_id:
            self._save_unmatched(source_kind, source, score)
            return None

        result = YandexTrack(
            str(candidate.id), album_id, candidate.title, _artists(candidate),
            getattr(candidate, "duration_ms", None), score.total,
        )
        self._save_match(source_kind, source, result, score)
        return result

    def yandex_match(self, source_kind: str, track: YandexTrack) -> SpotifyTrack | None:
        if cached := self._cached_spotify(track):
            return cached

        candidate, score = self._find_best_candidate(track, self._search_spotify, self._spotify_key)
        if not self._accepted(candidate, score):
            self._save_unmatched(source_kind, track, score)
            self._debug_rejected(candidate, score)
            return None

        if not candidate.source_id:
            self._save_unmatched(source_kind, track, score)
            return None

        self._save_match(source_kind, track, candidate, score)
        return candidate
