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
    cfg["playlists"] = normalize_bindings(cfg["playlists"])
    if len(cfg["playlists"]) != 1 or cfg["playlists"][0].get("source") != "likes":
        raise ValueError("Configure exactly one job with source: likes")
    binding = cfg["playlists"][0]
    if binding.get("sync_mode", "mirror") not in {"mirror", "add_only"}:
        raise ValueError("sync_mode must be mirror or add_only")
    binding.setdefault("initial_limit", 20)
    if not isinstance(binding["initial_limit"], int) or binding["initial_limit"] < 0:
        raise ValueError("initial_limit must be a non-negative integer")
    return cfg


def normalize_bindings(bindings: list[dict]) -> list[dict]:
    """Treat the old opposing likes bindings as one bidirectional job."""
    result, consumed = [], set()
    for i, binding in enumerate(bindings):
        if i in consumed:
            continue
        source = binding.get("source")
        opposite = {"spotify_liked": "yandex_likes", "yandex_likes": "spotify_liked"}.get(source)
        reverse = next((j for j, other in enumerate(bindings)
                        if j != i and j not in consumed and opposite
                        and binding.get("target", opposite) == opposite
                        and other.get("name") == binding.get("name")
                        and other.get("source") == opposite
                        and other.get("target", source) == source), None)
        if reverse is not None:
            if binding.get("sync_mode", "mirror") != bindings[reverse].get("sync_mode", "mirror"):
                raise ValueError("Opposing likes bindings must use the same sync_mode")
            result.append({"name": binding["name"], "source": "likes",
                           "sync_mode": binding.get("sync_mode", "mirror")})
            consumed.add(reverse)
        else:
            result.append(binding)
    names = [b["name"] for b in result]
    if len(names) != len(set(names)):
        raise ValueError("Sync binding names must be unique")
    return result
