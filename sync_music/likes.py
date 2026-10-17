from __future__ import annotations


def sync_likes(engine, binding: dict) -> dict:
    """Reconcile two snapshots against the last successful shared baseline."""
    name = binding["name"]
    mode = binding.get("sync_mode", "mirror")
    if mode not in {"mirror", "add_only"}:
        raise ValueError(f"{name}: unknown sync_mode {mode!r}")
    initial_limit = int(binding.get("initial_limit", 20))
    if initial_limit < 0:
        raise ValueError("initial_limit must be non-negative")
    previous, pending = engine.db.likes_state(name)

    def apply(plan):
        spotify = engine.spotify.sync_likes(set(plan["spotify"]), set(plan["remove_spotify"]))
        yandex = engine.target.sync_likes(set(plan["yandex"]), set(plan["remove_yandex"]))
        engine.db.save_likes_state(name, plan["pairs"])
        return spotify, yandex

    # Replay idempotently if a previous run stopped between the two services.
    recovered = False
    if pending is not None:
        results = apply(pending)
        recovered = any(r["changed"] for r in results)
        previous = pending["pairs"]

    spotify = {str(t.source_id): t for t in engine.spotify.liked_tracks()}
    yandex = {str(t.track_id): t for t in engine.target.get_likes()}
    ignored = engine.db.ignored_likes(name)
    if ignored is None:
        ignored = (set(list(spotify)[initial_limit:]), set(list(yandex)[initial_limit:]))
    ignored_spotify, ignored_yandex = ignored
    # An old track becomes eligible again only after observing its unlike.
    ignored_spotify &= set(spotify)
    ignored_yandex &= set(yandex)
    engine.db.save_ignored_likes(name, ignored_spotify, ignored_yandex)
    pairs = {}
    handled_spotify, handled_yandex = set(), set()
    remove_spotify, remove_yandex = set(), set()
    for sid, yid, album in previous:
        handled_spotify.add(sid)
        handled_yandex.add(yid)
        if mode == "mirror" and (sid not in spotify or yid not in yandex):
            remove_spotify.add(sid)
            remove_yandex.add(yid)
        else:
            pairs[sid] = (sid, yid, album)

    unmatched = 0
    for sid, track in spotify.items():
        if sid in handled_spotify or sid in ignored_spotify:
            continue
        ref = engine.matcher.match("spotify", track)
        if ref is None:
            unmatched += 1
            continue
        yid = str(ref.track_id)
        # Do not resurrect a removed pair through another edition of a song.
        if yid in remove_yandex:
            continue
        pairs[sid] = (sid, yid, str(ref.album_id))
        handled_yandex.add(yid)

    for yid, track in yandex.items():
        if yid in handled_yandex or yid in ignored_yandex:
            continue
        ref = engine.matcher.yandex_match("yandex", track)
        if ref is None:
            unmatched += 1
            continue
        sid = str(ref.source_id)
        if sid in remove_spotify:
            continue
        # A Spotify ID has one canonical counterpart; preserve other editions.
        pairs.setdefault(sid, (sid, yid, str(track.album_id)))

    plan = {
        "pairs": list(pairs.values()),
        "spotify": sorted(pairs),
        "yandex": sorted({row[1] for row in pairs.values()}),
        "remove_spotify": sorted(remove_spotify),
        "remove_yandex": sorted(remove_yandex),
    }
    # Persist the intent before the first remote write, never the success state.
    engine.db.save_likes_state(name, previous, plan)
    sp_result, ya_result = apply(plan)
    changed = recovered or bool(sp_result["changed"] or ya_result["changed"])
    print(
        f"[{name}] Spotify <-> Yandex: {len(pairs)} matched, {unmatched} unmatched; "
        f"Spotify +{sp_result['added']}/-{sp_result['removed']}, "
        f"Yandex +{ya_result['added']}/-{ya_result['removed']}"
    )
    return {
        "name": name, "source_count": len(spotify) + len(yandex),
        "matched_count": len(pairs), "unmatched_count": unmatched, "changed": changed,
    }
