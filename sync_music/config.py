from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent


def resolve_path(value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else ROOT / p


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    cfg_path = resolve_path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Не найден {cfg_path}. Выполни: python run.py init"
        )
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    spotify = cfg.setdefault("spotify", {})
    if os.getenv("SPOTIFY_CLIENT_ID"):
        spotify["client_id"] = os.environ["SPOTIFY_CLIENT_ID"]

    spotify.setdefault("redirect_uri", "http://127.0.0.1:8787/callback")
    spotify.setdefault("token_file", "data/spotify_token.json")
    spotify.setdefault("browser_profile", "data/spotify_browser_profile")
    spotify.setdefault("request_timeout_seconds", 30)

    yandex = cfg.setdefault("yandex", {})
    yandex.setdefault("token_file", "data/yandex_token.json")

    matching = cfg.setdefault("matching", {})
    matching.setdefault("min_score", 86)
    matching.setdefault("candidate_limit", 30)
    matching.setdefault("duration_tolerance_ms", 8000)
    matching.setdefault("high_confidence_score", 94)
    matching.setdefault("debug_unmatched", True)
    cfg.setdefault("playlists", [])
    return cfg
