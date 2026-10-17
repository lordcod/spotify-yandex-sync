import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yandex_music.exceptions import NetworkError, UnauthorizedError, BadRequestError, TimedOutError
from sync_music.matcher import Matcher


class SearchRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.matcher = Matcher(self.client, None, Mock(), {})
        self.now = 100.0
        self.clock = patch('sync_music.matcher.time.monotonic', side_effect=lambda: self.now)
        self.sleep = patch('sync_music.matcher.time.sleep', side_effect=self.advance)
        self.clock.start()
        self.sleeper = self.sleep.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.sleep.stop)

    def advance(self, seconds):
        self.now += seconds

    def test_429_retries_same_query_then_returns_results(self):
        self.client.search.side_effect = [NetworkError("Unknown HTTPError (429): b'Concurrency limit exceeded'"),
                                          SimpleNamespace(tracks=SimpleNamespace(results=['track']))]
        self.assertEqual(self.matcher._search_yandex('artist song'), ['track'])
        self.assertEqual(self.client.search.call_count, 2)
        self.assertEqual(self.client.search.call_args_list[0], self.client.search.call_args_list[1])
        self.assertEqual([c.args[0] for c in self.sleeper.call_args_list], [2, 1])

    def test_exhaustion_propagates_without_marking_unmatched(self):
        error = NetworkError('Unknown HTTPError (429): limit')
        self.client.search.side_effect = error
        with self.assertRaises(NetworkError) as raised:
            self.matcher._search_yandex('song')
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.search.call_count, 6)
        self.assertEqual([c.args[0] for c in self.sleeper.call_args_list if c.args[0] > 1], [2, 4, 8, 16, 32])
        self.matcher.db.save_mapping.assert_not_called()

    def test_permanent_errors_not_retried(self):
        for error in [UnauthorizedError('forbidden'), BadRequestError('bad query'), NetworkError('Unknown HTTPError (418): nope')]:
            self.client.search.reset_mock()
            self.client.search.side_effect = error
            with self.assertRaises(type(error)):
                self.matcher._search_yandex('song')
            self.assertEqual(self.client.search.call_count, 1)

    def test_transient_server_errors_and_timeout_retried(self):
        for error in [NetworkError('Bad Gateway'), NetworkError('Unknown HTTPError (503): unavailable'), TimedOutError()]:
            self.client.search.reset_mock()
            self.client.search.side_effect = [error, SimpleNamespace(tracks=None)]
            self.assertEqual(self.matcher._search_yandex('song'), [])
            self.assertEqual(self.client.search.call_count, 2)

    def test_successful_searches_are_paced(self):
        self.client.search.return_value = SimpleNamespace(tracks=None)
        self.matcher._search_yandex('first')
        self.matcher._search_yandex('second')
        self.sleeper.assert_called_once_with(1.0)


if __name__ == '__main__':
    unittest.main()
