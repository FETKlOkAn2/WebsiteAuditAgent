"""
Tests for Serper multi-key rotation (improvement: scale past one key's quota).
Fully offline — env vars patched, no network.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.discovery.serper_keys import (  # noqa: E402
    SerperKeyPool, load_serper_keys, get_serper_pool, reset_serper_pool,
)


# ---------------------------------------------------------------------------
# load_serper_keys
# ---------------------------------------------------------------------------

class TestLoadKeys(unittest.TestCase):

    def _clear(self):
        for k in list(os.environ):
            if k.startswith("SERPER_API_KEY"):
                del os.environ[k]

    def setUp(self):
        self._saved = {k: v for k, v in os.environ.items() if k.startswith("SERPER_API_KEY")}
        self._clear()

    def tearDown(self):
        self._clear()
        os.environ.update(self._saved)

    def test_single_key(self):
        os.environ["SERPER_API_KEY"] = "k1"
        self.assertEqual(load_serper_keys(), ["k1"])

    def test_comma_separated(self):
        os.environ["SERPER_API_KEY"] = "k1, k2 ,k3"
        self.assertEqual(load_serper_keys(), ["k1", "k2", "k3"])

    def test_numbered_keys(self):
        os.environ["SERPER_API_KEY"] = "k1"
        os.environ["SERPER_API_KEY_2"] = "k2"
        os.environ["SERPER_API_KEY_3"] = "k3"
        self.assertEqual(load_serper_keys(), ["k1", "k2", "k3"])

    def test_dedupes_and_orders(self):
        os.environ["SERPER_API_KEY"] = "k1,k1"
        os.environ["SERPER_API_KEY_2"] = "k1"
        os.environ["SERPER_API_KEY_3"] = "k2"
        self.assertEqual(load_serper_keys(), ["k1", "k2"])

    def test_numbered_stops_at_gap(self):
        os.environ["SERPER_API_KEY"] = "k1"
        os.environ["SERPER_API_KEY_2"] = "k2"
        # no _3 -> _4 must not be picked up
        os.environ["SERPER_API_KEY_4"] = "k4"
        self.assertEqual(load_serper_keys(), ["k1", "k2"])

    def test_empty(self):
        self.assertEqual(load_serper_keys(), [])


# ---------------------------------------------------------------------------
# SerperKeyPool
# ---------------------------------------------------------------------------

class TestPool(unittest.TestCase):

    def test_current_is_first(self):
        self.assertEqual(SerperKeyPool(["a", "b"]).current(), "a")

    def test_rotation_on_exhaustion(self):
        pool = SerperKeyPool(["a", "b", "c"])
        self.assertEqual(pool.current(), "a")
        pool.mark_exhausted("a")
        self.assertEqual(pool.current(), "b")
        pool.mark_exhausted("b")
        self.assertEqual(pool.current(), "c")
        pool.mark_exhausted("c")
        self.assertIsNone(pool.current())
        self.assertFalse(pool.has_key())

    def test_active_count(self):
        pool = SerperKeyPool(["a", "b"])
        self.assertEqual(pool.active(), 2)
        pool.mark_exhausted("a")
        self.assertEqual(pool.active(), 1)
        self.assertEqual(pool.total(), 2)

    def test_dedupe(self):
        self.assertEqual(SerperKeyPool(["a", "a", "b"]).total(), 2)

    def test_empty_pool(self):
        self.assertFalse(SerperKeyPool([]).has_key())

    def test_status_string(self):
        pool = SerperKeyPool(["a", "b"])
        pool.mark_exhausted("a")
        self.assertIn("1/2", pool.status())


# ---------------------------------------------------------------------------
# search_google_serp rotation
# ---------------------------------------------------------------------------

def _resp(status, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload or {}
    r.text = text
    return r


class TestSearchRotation(unittest.TestCase):

    def setUp(self):
        os.environ["SERPER_API_KEY"] = "k1"
        os.environ["SERPER_API_KEY_2"] = "k2"
        os.environ.pop("SERPER_API_KEY_3", None)
        reset_serper_pool()

    def tearDown(self):
        for k in ("SERPER_API_KEY", "SERPER_API_KEY_2"):
            os.environ.pop(k, None)
        reset_serper_pool()

    def test_first_key_works(self):
        from waa.discovery import prospector
        ok = _resp(200, {"organic": [{"link": "https://x.sk", "title": "X", "snippet": "s"}]})
        with patch.object(prospector.requests, "post", return_value=ok) as post:
            results = prospector.search_google_serp("dentist", num_results=5)
        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-KEY"], "k1")

    def test_rotates_to_second_key_on_credit_exhaustion(self):
        from waa.discovery import prospector
        exhausted = _resp(400, text="Not enough credits")
        ok = _resp(200, {"organic": [{"link": "https://x.sk", "title": "X", "snippet": "s"}]})
        with patch.object(prospector.requests, "post",
                          side_effect=[exhausted, ok]) as post:
            results = prospector.search_google_serp("dentist")
        self.assertEqual(len(results), 1)
        self.assertEqual(post.call_count, 2)
        # second attempt used the second key
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["X-API-KEY"], "k1")
        self.assertEqual(post.call_args_list[1].kwargs["headers"]["X-API-KEY"], "k2")

    def test_exhausted_key_stays_exhausted_next_query(self):
        from waa.discovery import prospector
        exhausted = _resp(403, text="forbidden")
        ok = _resp(200, {"organic": []})
        with patch.object(prospector.requests, "post",
                          side_effect=[exhausted, ok]):
            prospector.search_google_serp("q1")
        # k1 is now spent; the next query should go straight to k2 (1 call)
        with patch.object(prospector.requests, "post", return_value=ok) as post2:
            prospector.search_google_serp("q2")
        self.assertEqual(post2.call_count, 1)
        self.assertEqual(post2.call_args.kwargs["headers"]["X-API-KEY"], "k2")

    def test_all_exhausted_returns_empty(self):
        from waa.discovery import prospector
        exhausted = _resp(400, text="no credits")
        with patch.object(prospector.requests, "post", return_value=exhausted) as post:
            results = prospector.search_google_serp("q")
        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 2)  # tried both keys, then gave up

    def test_explicit_key_bypasses_pool(self):
        from waa.discovery import prospector
        ok = _resp(200, {"organic": []})
        with patch.object(prospector.requests, "post", return_value=ok) as post:
            prospector.search_google_serp("q", api_key="explicit")
        self.assertEqual(post.call_args.kwargs["headers"]["X-API-KEY"], "explicit")

    def test_rate_limit_does_not_burn_other_keys(self):
        from waa.discovery import prospector
        rl = _resp(429)
        with patch.object(prospector.requests, "post", return_value=rl) as post:
            results = prospector.search_google_serp("q")
        self.assertEqual(results, [])
        self.assertEqual(post.call_count, 1)  # 429 is transient -> stop, don't rotate


if __name__ == "__main__":
    unittest.main(verbosity=2)
