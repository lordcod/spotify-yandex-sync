from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .config import ROOT, load_config, resolve_path
from .daily_mix import SpotifyWebDailyMix
from .spotify import SpotifyClient
from .syncer import SyncEngine
from .yandex import authorize_yandex


def cmd_init(_args):
    src = ROOT / "config.example.yaml"
    dst = ROOT / "config.yaml"
    if dst.exists():
        print(f"Уже существует: {dst}")
    else:
        shutil.copy2(src, dst)
        print(f"Создан: {dst}")
    (ROOT / "data").mkdir(exist_ok=True)
    print("Теперь открой config.yaml и вставь Spotify Client ID.")


def cmd_auth(args):
    cfg = load_config(args.config)
    if args.service == "spotify":
        sc = cfg["spotify"]
        SpotifyClient(
            client_id=sc.get("client_id", ""),
            redirect_uri=sc["redirect_uri"],
            token_file=resolve_path(sc["token_file"]),
            timeout=int(sc.get("request_timeout_seconds", 30)),
        ).authorize_pkce()
    elif args.service == "spotify-web":
        SpotifyWebDailyMix(resolve_path(cfg["spotify"]["browser_profile"])).setup_login()
    elif args.service == "yandex":
        authorize_yandex(resolve_path(cfg["yandex"]["token_file"]))


def cmd_sync(args):
    cfg = load_config(args.config)
    engine = SyncEngine(cfg)
    try:
        engine.sync(args.name)
    finally:
        engine.close()


def cmd_list(args):
    cfg = load_config(args.config)
    for b in cfg.get("playlists", []):
        extra = b.get("source_id") or b.get("source_url") or ""
        target = b.get("target")
        if not target:
            target = "yandex_likes" if b.get("source") == "spotify_liked" else "yandex_playlist"
        target_label = "Yandex Мне нравится" if target == "yandex_likes" else b.get("target_title")
        print(f"{b.get('name')}: {b.get('source')} -> {target_label} {extra}")


def cmd_spotify_playlists(args):
    cfg = load_config(args.config)
    sc = cfg["spotify"]
    client = SpotifyClient(
        client_id=sc.get("client_id", ""),
        redirect_uri=sc["redirect_uri"],
        token_file=resolve_path(sc["token_file"]),
        timeout=int(sc.get("request_timeout_seconds", 30)),
    )
    for p in client.current_user_playlists():
        owner = (p.get("owner") or {}).get("display_name") or (p.get("owner") or {}).get("id") or "?"
        print(f"{p.get('name')}\n  id: {p.get('id')}\n  owner: {owner}\n")


def main():
    parser = argparse.ArgumentParser(description="Spotify -> Yandex Music sync")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config.yaml")
    p_init.set_defaults(func=cmd_init)

    p_auth = sub.add_parser("auth", help="authorize a service")
    p_auth.add_argument("service", choices=["spotify", "spotify-web", "yandex"])
    p_auth.set_defaults(func=cmd_auth)

    p_sync = sub.add_parser("sync", help="sync one binding or all")
    p_sync.add_argument("name", nargs="?", default="all")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="list configured bindings")
    p_list.set_defaults(func=cmd_list)

    p_sp = sub.add_parser("spotify-playlists", help="list your Spotify playlists and IDs")
    p_sp.set_defaults(func=cmd_spotify_playlists)

    args = parser.parse_args()
    args.func(args)
