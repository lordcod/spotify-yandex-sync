from __future__ import annotations

import argparse
import shutil

from .config import ROOT, load_config, resolve_path
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
    for b in cfg["playlists"]:
        print(f"{b['name']}: Spotify <-> Yandex likes ({b.get('sync_mode', 'mirror')}, initial limit {b.get('initial_limit', 20)})")


def main():
    parser = argparse.ArgumentParser(description="Spotify <-> Yandex Music sync")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config.yaml")
    p_init.set_defaults(func=cmd_init)

    p_auth = sub.add_parser("auth", help="authorize a service")
    p_auth.add_argument("service", choices=["spotify", "yandex"])
    p_auth.set_defaults(func=cmd_auth)

    p_sync = sub.add_parser("sync", help="sync one binding or all")
    p_sync.add_argument("name", nargs="?", default="all")
    p_sync.set_defaults(func=cmd_sync)

    p_list = sub.add_parser("list", help="list configured bindings")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)
