import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sync_music.config import normalize_bindings
from sync_music.db import Database
from sync_music.likes import sync_likes
from sync_music.matcher import Matcher
from sync_music.models import SpotifyTrack, YandexTrack
from sync_music.spotify import SpotifyClient
from sync_music.yandex import YandexTarget


class LikesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / 'sync.db')
        self.sp = {'a'}
        self.ya = {'b'}
        self.fail_yandex = False
        self.engine = SimpleNamespace(db=self.db, spotify=Mock(), target=Mock(), matcher=Mock())
        self.engine.spotify.liked_tracks.side_effect = lambda: [SpotifyTrack(x, x, ['artist']) for x in self.sp]
        self.engine.target.get_likes.side_effect = lambda: [YandexTrack(x, 'album', x, ['artist'], 1000) for x in self.ya]
        self.engine.matcher.match.side_effect = lambda kind, t: YandexTrack(t.source_id, 'album', t.title, t.artists, 1000)
        self.engine.matcher.yandex_match.side_effect = lambda kind, t: SpotifyTrack(t.track_id, t.title, t.artists)
        self.engine.spotify.sync_likes.side_effect = lambda desired, remove: self.apply(self.sp, desired, remove)
        self.engine.target.sync_likes.side_effect = self.apply_yandex
        self.binding = dict(name='liked', source='likes', sync_mode='mirror')

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    @staticmethod
    def apply(current, desired, remove):
        added, removed = desired - current, (current & remove) - desired
        current.difference_update(removed)
        current.update(added)
        return dict(added=len(added), removed=len(removed), changed=bool(added or removed))

    def apply_yandex(self, desired, remove):
        if self.fail_yandex:
            raise RuntimeError('offline')
        return self.apply(self.ya, desired, remove)

    def sync(self):
        return sync_likes(self.engine, self.binding)

    def test_different_service_ids_and_engine_entrypoint(self):
        from sync_music.syncer import SyncEngine
        self.sp = {'sp1'}
        self.ya = {'ya2'}
        self.engine.matcher.match.side_effect = lambda kind, t: YandexTrack(t.source_id.replace('sp', 'ya'), 'album', t.title, t.artists, 1000)
        self.engine.matcher.yandex_match.side_effect = lambda kind, t: SpotifyTrack(t.track_id.replace('ya', 'sp'), t.title, t.artists)
        engine = SyncEngine.__new__(SyncEngine)
        engine.__dict__.update(vars(self.engine))
        engine.cfg = {'playlists': [self.binding]}
        self.assertTrue(engine.sync('liked')[0]['changed'])
        self.assertEqual(self.sp, {'sp1', 'sp2'})
        self.assertEqual(self.ya, {'ya1', 'ya2'})
        self.ya.remove('ya1')
        engine.sync('all')
        self.assertEqual(self.sp, {'sp2'})
        self.assertEqual(self.ya, {'ya2'})
        self.assertEqual(self.db.conn.execute("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0], 'ok')

    def test_initial_limit_does_not_import_old_tracks_on_later_runs(self):
        self.sp = {f's{i}' for i in range(30)}
        self.ya = {f'y{i}' for i in range(30)}
        self.engine.spotify.liked_tracks.side_effect = lambda: [SpotifyTrack(x, x, ['artist']) for x in sorted(self.sp)]
        self.engine.target.get_likes.side_effect = lambda: [YandexTrack(x, 'album', x, ['artist'], 1000) for x in sorted(self.ya)]
        self.assertEqual(self.sync()['matched_count'], 40)
        self.assertEqual(len(self.sp), 50)
        self.assertEqual(len(self.ya), 50)
        self.assertFalse(self.sync()['changed'])
        self.sp.add('new')
        self.sync()
        self.assertIn('new', self.ya)
        self.assertEqual(len(self.ya), 51)

    def test_future_additions_are_not_limited_to_twenty(self):
        self.sync()
        new_tracks = {f'future{i}' for i in range(25)}
        self.ya.update(new_tracks)
        self.sync()
        self.assertTrue(new_tracks <= self.sp)

    def test_rollback_exclusions_prevent_reimport_and_yandex_removal(self):
        self.sp = {'kept'}
        self.ya = {'kept', 'rolled_back'}
        self.db.save_likes_state('liked', [['kept', 'kept', 'album']])
        self.db.save_ignored_likes('liked', {'kept'}, {'kept', 'rolled_back'})
        self.assertFalse(self.sync()['changed'])
        self.assertEqual(self.sp, {'kept'})
        self.assertIn('rolled_back', self.ya)
        self.ya.add('new')
        self.sync()
        self.assertEqual(self.sp, {'kept', 'new'})

    def test_ignored_track_can_be_reliked_after_observed_unlike(self):
        self.binding['initial_limit'] = 0
        self.sync()
        self.assertEqual(self.sp, {'a'})
        self.assertEqual(self.ya, {'b'})
        self.ya.remove('b')
        self.sync()
        self.ya.add('b')
        self.sync()
        self.assertIn('b', self.sp)

    def test_initial_union_and_idempotence(self):
        self.assertTrue(self.sync()['changed'])
        self.assertEqual(self.sp, {'a', 'b'})
        self.assertEqual(self.ya, self.sp)
        self.assertFalse(self.sync()['changed'])

    def test_removals_both_directions_and_relike(self):
        self.sync()
        self.sp.remove('a')
        self.ya.remove('b')
        self.sync()
        self.assertEqual(self.sp, set())
        self.assertEqual(self.ya, set())
        self.ya.add('a')
        self.sync()
        self.assertEqual(self.sp, {'a'})
        self.assertFalse(self.sync()['changed'])

    def test_new_likes_both_directions(self):
        self.sync()
        self.sp.add('c')
        self.ya.add('d')
        self.sync()
        self.assertEqual(self.sp, {'a', 'b', 'c', 'd'})
        self.assertEqual(self.sp, self.ya)

    def test_add_only_restores_unliked_track(self):
        self.sync()
        self.binding['sync_mode'] = 'add_only'
        self.sp.remove('b')
        self.sync()
        self.assertEqual(self.sp, self.ya)

    def test_partial_add_failure_is_replayed(self):
        self.fail_yandex = True
        with self.assertRaises(RuntimeError):
            self.sync()
        previous, pending = self.db.likes_state('liked')
        self.assertEqual(previous, [])
        self.assertIsNotNone(pending)
        self.fail_yandex = False
        self.sync()
        self.assertEqual(self.sp, {'a', 'b'})
        self.assertEqual(self.ya, self.sp)
        self.assertIsNone(self.db.likes_state('liked')[1])

    def test_partial_remove_failure_is_replayed(self):
        self.sync()
        self.sp.remove('a')
        self.ya.remove('b')
        self.fail_yandex = True
        with self.assertRaises(RuntimeError):
            self.sync()
        self.fail_yandex = False
        self.sync()
        self.assertEqual(self.sp, set())
        self.assertEqual(self.ya, set())

    def test_search_failure_does_not_write_libraries(self):
        self.engine.matcher.match.side_effect = RuntimeError('search unavailable')
        with self.assertRaises(RuntimeError):
            self.sync()
        self.engine.spotify.sync_likes.assert_not_called()
        self.engine.target.sync_likes.assert_not_called()

    def test_unmatched_is_preserved_and_retried(self):
        self.engine.matcher.yandex_match.side_effect = None
        self.engine.matcher.yandex_match.return_value = None
        self.assertEqual(self.sync()['unmatched_count'], 1)
        self.assertIn('b', self.ya)
        self.assertEqual(self.sync()['unmatched_count'], 1)

    def test_cache_upsert_on_fresh_and_legacy_schema(self):
        for legacy in (False, True):
            if legacy:
                self.db.conn.execute('DROP TABLE track_map')
                from sync_music.db import SCHEMA
                self.db.conn.executescript(SCHEMA.replace(
                    'PRIMARY KEY (source_kind, source_track_id, yandex_track_id)',
                    'PRIMARY KEY (source_kind, source_track_id)'))
            for _ in range(2):
                self.db.save_mapping('yandex', 'sp', 'title', 'artist', 1000, 'ya', 'album', 'title', 'artist', 99, 'matched')
                self.db.save_mapping('yandex', None, None, None, None, 'other', 'album', 'title', 'artist', None, 'unmatched')
            self.assertEqual(self.db.conn.execute('SELECT COUNT(*) FROM track_map').fetchone()[0], 2)
            matcher = Matcher(None, None, self.db, {})
            self.assertEqual(matcher._cached_spotify(YandexTrack('ya', 'album', '', [], None)).artists, ['artist'])

    def test_search_errors_are_not_unmatched(self):
        client = Mock()
        client.search.side_effect = RuntimeError('HTTP 403')
        matcher = Matcher(None, client, self.db, {})
        with self.assertRaisesRegex(RuntimeError, '403'):
            matcher.yandex_match('yandex', YandexTrack('ya', 'album', 'title', ['artist'], 1000))

    def test_legacy_config_pair(self):
        bindings = [dict(name='liked', source='spotify_liked', target='yandex_likes', sync_mode='mirror'),
                    dict(name='liked', source='yandex_likes', target='spotify_liked', sync_mode='mirror')]
        self.assertEqual(normalize_bindings(bindings), [self.binding])
        self.assertEqual(normalize_bindings(list(reversed(bindings))), [self.binding])


class ClientTests(unittest.TestCase):
    def test_spotify_sends_library_writes(self):
        client = SpotifyClient('client', 'http://127.0.0.1', Path('unused'))
        client.liked_tracks = Mock(return_value=[SpotifyTrack('old', '', [])])
        client._access_token = Mock(return_value='fake')
        client.session = Mock()
        client.session.request.return_value.status_code = 200
        client.session.request.return_value.ok = True
        result = client.sync_likes({'new'}, {'old'})
        self.assertEqual(result['added'], 1)
        calls = client.session.request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ['PUT', 'DELETE'])
        self.assertEqual(calls[0].kwargs['params'], {'uris': 'spotify:track:new'})

    def test_yandex_ids_are_strings_and_metadata_is_batched(self):
        client = Mock()
        target = YandexTarget(client)
        target.ordered_liked_track_ids = Mock(return_value=[str(i) for i in range(205)])
        client.tracks.side_effect = lambda batch: [SimpleNamespace(id=int(i), albums=[], title='x', artists=[], duration_ms=1000) for i in batch]
        tracks = target.get_likes()
        self.assertEqual(len(tracks), 205)
        self.assertTrue(all(isinstance(t.track_id, str) for t in tracks))
        self.assertEqual(client.tracks.call_count, 3)

    def test_yandex_newest_likes_keep_order_through_metadata_fetch(self):
        client = Mock()
        client.users_likes_tracks.return_value = SimpleNamespace(tracks=[
            SimpleNamespace(id='old', timestamp='2020-01-01T00:00:00Z'),
            SimpleNamespace(id='new', timestamp='2026-10-17T00:00:00Z')])
        client.tracks.return_value = [SimpleNamespace(id=x, albums=[], title=x, artists=[], duration_ms=1000) for x in ['old', 'new']]
        self.assertEqual([t.track_id for t in YandexTarget(client).get_likes()], ['new', 'old'])

    def test_empty_yandex_response_is_failure(self):
        client = Mock()
        client.users_likes_tracks.return_value = None
        with self.assertRaises(RuntimeError):
            YandexTarget(client).liked_track_ids()

    def test_desired_yandex_track_is_not_removed(self):
        client = Mock()
        target = YandexTarget(client)
        target.liked_track_ids = Mock(return_value={'same'})
        self.assertFalse(target.sync_likes({'same'}, {'same'})['changed'])
        client.users_likes_tracks_remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
