from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .db import Database
from .models import SourceTrack, YandexTrackRef


# Things that often differ between Spotify/Yandex metadata but do not necessarily
# mean it is a different recording. We strip them for the main title comparison;
# duration still protects us from matching a random live/cover version.
BRACKET_NOISE = re.compile(
    r"\s*[\(\[](?:"
    r"remaster(?:ed)?(?:\s*\d{2,4})?|"
    r"radio\s+edit|edit|version|deluxe|bonus|explicit|clean|mono|stereo|"
    r"original\s+mix|single\s+version"
    r")[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
FEAT = re.compile(r"\s+(?:feat\.?|ft\.?)\s+.*$", re.IGNORECASE)
NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
SPACES = re.compile(r"\s+")

# A Spotify catalogue quite often romanizes Russian artist names while Yandex
# stores them in Cyrillic. This transliteration intentionally follows the common
# English-style forms (Моя -> Moya, Сергей -> Sergey, Юлия -> Yuliya) rather than
# a strict scientific standard.
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


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    text = BRACKET_NOISE.sub("", text)
    text = FEAT.sub("", text)
    text = NON_WORD.sub(" ", text)
    return SPACES.sub(" ", text).strip()


def transliterate_ru(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(RU_TO_LAT.get(ch, ch) for ch in text)


def latinize(text: str) -> str:
    """Normalize and transliterate Cyrillic so Latin/Cyrillic names can match."""
    return normalize(transliterate_ru(text))


def _variants(text: str) -> tuple[str, ...]:
    normal = normalize(text)
    latin = latinize(text)
    values: list[str] = []
    for value in (normal, latin):
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _text_similarity(left: str, right: str) -> float:
    best = 0.0
    for a in _variants(left):
        for b in _variants(right):
            # ratio is important for titles; token_set handles punctuation/order
            # differences in artist credits and multi-word names.
            best = max(best, float(fuzz.ratio(a, b)), float(fuzz.token_set_ratio(a, b)))
    return best


def _artists(track) -> list[str]:
    try:
        return [x for x in track.artists_name() if x]
    except Exception:
        return [a.name for a in (getattr(track, "artists", None) or []) if getattr(a, "name", None)]


def _album_titles(track) -> list[str]:
    result: list[str] = []
    for album in getattr(track, "albums", None) or []:
        title = getattr(album, "title", None)
        if title:
            result.append(str(title))
    return result


def _artist_similarity(source_artists: list[str], target_artists: list[str]) -> float:
    if not source_artists:
        return 70.0
    if not target_artists:
        return 0.0

    # Each Spotify artist should have a good counterpart on Yandex. Extra Yandex
    # credits are only a small penalty because catalogues often credit features
    # differently.
    source_coverage = sum(
        max(_text_similarity(src, dst) for dst in target_artists)
        for src in source_artists
    ) / len(source_artists)
    target_coverage = sum(
        max(_text_similarity(dst, src) for src in source_artists)
        for dst in target_artists
    ) / len(target_artists)
    return source_coverage * 0.88 + target_coverage * 0.12


def _duration_similarity(source_ms: int | None, target_ms: int | None, tolerance_ms: int) -> float:
    if not source_ms or not target_ms:
        return 70.0
    delta = abs(int(source_ms) - int(target_ms))
    if delta <= 1500:
        return 100.0
    if delta <= 3000:
        return 97.0
    if delta <= tolerance_ms:
        # Smoothly decrease from ~97 to 70 inside the configured tolerance.
        span = max(1, tolerance_ms - 3000)
        return 97.0 - ((delta - 3000) / span) * 27.0
    if delta <= 15000:
        return 45.0
    if delta <= 30000:
        return 20.0
    return 5.0


def score_candidate(source: SourceTrack, candidate, duration_tolerance_ms: int) -> CandidateScore:
    title_score = _text_similarity(source.title, str(getattr(candidate, "title", "") or ""))
    artist_score = _artist_similarity(source.artists, _artists(candidate))
    duration_score = _duration_similarity(
        source.duration_ms,
        getattr(candidate, "duration_ms", None),
        duration_tolerance_ms,
    )

    album_score = 70.0
    if source.album:
        albums = _album_titles(candidate)
        if albums:
            album_score = max(_text_similarity(source.album, album) for album in albums)

    # Title/artist decide identity; duration is a strong guard against covers,
    # live takes and wrong recordings. Album is intentionally weak because
    # compilations/reissues differ between services.
    total = (
        title_score * 0.45
        + artist_score * 0.32
        + duration_score * 0.20
        + album_score * 0.03
    )

    # A candidate with a substantially different title or no artist agreement
    # should never pass merely because duration happens to be close.
    if title_score < 70:
        total -= (70 - title_score) * 0.35
    if artist_score < 70:
        total -= (55 - artist_score) * 0.35

    return CandidateScore(
        total=max(0.0, total),
        title=title_score,
        artist=artist_score,
        duration=duration_score,
        album=album_score,
    )


class Matcher:
    def __init__(self, yandex_client, db: Database, cfg: dict):
        self.client = yandex_client
        self.db = db
        self.min_score = float(cfg.get("min_score", 86))
        self.high_confidence_score = float(cfg.get("high_confidence_score", 94))
        self.candidate_limit = int(cfg.get("candidate_limit", 30))
        self.duration_tolerance_ms = int(cfg.get("duration_tolerance_ms", 8000))
        self.debug_unmatched = bool(cfg.get("debug_unmatched", True))

    @staticmethod
    def _candidate_key(candidate) -> tuple[str, str]:
        track_id = str(getattr(candidate, "id", "") or "")
        albums = getattr(candidate, "albums", None) or []
        album_id = str(getattr(albums[0], "id", "") or "") if albums else ""
        return track_id, album_id

    def _search_queries(self, source: SourceTrack) -> list[str]:
        artists = [a.strip() for a in source.artists[:2] if a and a.strip()]
        main_artist = artists[0] if artists else ""
        queries = [
            " ".join([*artists, source.title]).strip(),
            " ".join([source.title, main_artist]).strip(),
            source.title.strip(),
        ]

        # If the source itself contains Cyrillic, also try a Latin query because
        # some catalogue entries are stored under a romanized artist/title.
        latin_query = latinize(" ".join([*artists, source.title]))
        original_norm = normalize(" ".join([*artists, source.title]))
        if latin_query and latin_query != original_norm:
            queries.append(latin_query)

        unique: list[str] = []
        seen: set[str] = set()
        for query in queries:
            key = normalize(query)
            if query and key and key not in seen:
                unique.append(query)
                seen.add(key)
        return unique

    def _search(self, query: str) -> list:
        try:
            search = self.client.search(query, type_="track")
        except Exception:
            return []
        tracks_page = getattr(search, "tracks", None)
        results = getattr(tracks_page, "results", None) if tracks_page else None
        return list(results or [])[: self.candidate_limit]

    def _best(self, source: SourceTrack, candidates: list) -> tuple[object | None, CandidateScore | None]:
        best_obj = None
        best_score: CandidateScore | None = None
        for candidate in candidates:
            value = score_candidate(source, candidate, self.duration_tolerance_ms)
            if best_score is None or value.total > best_score.total:
                best_obj, best_score = candidate, value
        return best_obj, best_score

    def match(self, source_kind: str, source: SourceTrack) -> YandexTrackRef | None:
        cached = self.db.get_mapping(source_kind, source.source_id)
        if cached and cached["status"] == "matched" and cached["yandex_track_id"]:
            return YandexTrackRef(
                track_id=cached["yandex_track_id"],
                album_id=cached["yandex_album_id"],
                title=cached["yandex_title"] or source.title,
                artists=(cached["yandex_artists"] or "").split(" | ") if cached["yandex_artists"] else [],
                duration_ms=None,
                score=float(cached["match_score"] or 100),
            )

        candidates_by_key: dict[tuple[str, str], object] = {}
        best_obj = None
        best_score: CandidateScore | None = None

        # Progressive fallback: common tracks normally succeed on the first query,
        # while troublesome transliterated names fall back to title-only search.
        for query in self._search_queries(source):
            for candidate in self._search(query):
                key = self._candidate_key(candidate)
                if key[0]:
                    candidates_by_key.setdefault(key, candidate)

            best_obj, best_score = self._best(source, list(candidates_by_key.values()))
            if best_score and best_score.total >= self.high_confidence_score:
                break

        if best_obj is None or best_score is None or best_score.total < self.min_score:
            self.db.save_mapping(
                source_kind, source.source_id, source.title, source.artist_text,
                source.duration_ms, None, None, None, None,
                None if best_score is None else best_score.total, "unmatched"
            )
            if self.debug_unmatched and best_obj is not None and best_score is not None:
                print(
                    "[matcher] best rejected "
                    f"{best_score.total:.1f} "
                    f"(title={best_score.title:.0f}, artist={best_score.artist:.0f}, "
                    f"duration={best_score.duration:.0f}): "
                    f"{', '.join(_artists(best_obj))} — {getattr(best_obj, 'title', '?')}"
                )
            return None

        albums = getattr(best_obj, "albums", None) or []
        album_id = str(albums[0].id) if albums else ""
        if not album_id:
            self.db.save_mapping(
                source_kind, source.source_id, source.title, source.artist_text,
                source.duration_ms, None, None, None, None, best_score.total, "unmatched"
            )
            return None

        ref = YandexTrackRef(
            track_id=str(best_obj.id),
            album_id=album_id,
            title=best_obj.title,
            artists=_artists(best_obj),
            duration_ms=getattr(best_obj, "duration_ms", None),
            score=best_score.total,
        )
        self.db.save_mapping(
            source_kind, source.source_id, source.title, source.artist_text,
            source.duration_ms, ref.track_id, ref.album_id, ref.title,
            " | ".join(ref.artists), ref.score, "matched"
        )
        return ref
