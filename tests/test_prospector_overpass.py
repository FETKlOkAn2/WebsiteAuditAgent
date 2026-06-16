"""
Tests for the OpenStreetMap / Overpass prospect source.

All offline — requests.post is mocked. Covers the niche/area mapping, the
tag/website extraction, dedup, the unmapped-niche short-circuit, and the
429 retry-then-give-up path.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import waa.discovery.prospector as prospector  # noqa: E402


def _resp(status=200, elements=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"elements": elements or []}
    return r


SAMPLE = [
    {"tags": {"name": "Mlyn", "amenity": "restaurant",
              "website": "https://www.mlyn.sk", "phone": "+421 903 000 000",
              "addr:street": "Hlavná", "addr:housenumber": "1", "addr:city": "Bratislava"}},
    {"tags": {"name": "Koliba", "amenity": "restaurant",
              "contact:website": "koliba.sk"}},                      # no scheme, contact:website
    {"tags": {"name": "No Web", "amenity": "restaurant"}},           # dropped: no website
    {"tags": {"name": "Dup", "amenity": "restaurant",
              "website": "https://www.mlyn.sk/menu"}},               # dropped: same domain as Mlyn
]


class TestSearchOverpass(unittest.TestCase):

    def test_unmapped_niche_returns_empty_without_calling(self):
        with patch("waa.discovery.prospector.requests.post") as post:
            out = prospector.search_overpass("spaceship repair", "Bratislava")
        self.assertEqual(out, [])
        post.assert_not_called()

    def test_no_location_returns_empty(self):
        self.assertEqual(prospector.search_overpass("restauracia", ""), [])

    def test_parses_websites_and_dedups(self):
        with patch("waa.discovery.prospector.requests.post", return_value=_resp(200, SAMPLE)):
            out = prospector.search_overpass("restauracia", "Bratislava")
        urls = [r["url"] for r in out]
        self.assertIn("https://www.mlyn.sk", urls)
        self.assertIn("https://koliba.sk", urls)        # scheme added
        self.assertEqual(len(out), 2)                    # No-Web dropped, Dup deduped

    def test_carries_phone_and_address(self):
        with patch("waa.discovery.prospector.requests.post", return_value=_resp(200, SAMPLE)):
            out = prospector.search_overpass("restauracia", "Bratislava")
        mlyn = next(r for r in out if r["title"] == "Mlyn")
        self.assertEqual(mlyn["phone"], "+421 903 000 000")
        self.assertIn("Bratislava", mlyn["address"])

    def test_ascii_city_maps_to_osm_name(self):
        captured = {}

        def fake_post(url, data=None, **kw):
            captured["query"] = data["data"]
            return _resp(200, [])

        with patch("waa.discovery.prospector.requests.post", side_effect=fake_post):
            prospector.search_overpass("zubar", "Kosice")
        self.assertIn('"name"="Košice"', captured["query"])

    def test_429_retries_then_gives_up(self):
        with patch("waa.discovery.prospector.requests.post", return_value=_resp(429)) as post, \
             patch("waa.discovery.prospector.time.sleep"):  # don't actually wait
            out = prospector.search_overpass("restauracia", "Bratislava")
        self.assertEqual(out, [])
        self.assertEqual(post.call_count, 3)  # initial + 2 retries

    def test_429_then_success(self):
        with patch("waa.discovery.prospector.requests.post",
                   side_effect=[_resp(429), _resp(200, SAMPLE)]) as post, \
             patch("waa.discovery.prospector.time.sleep"):
            out = prospector.search_overpass("restauracia", "Bratislava")
        self.assertEqual(len(out), 2)
        self.assertEqual(post.call_count, 2)

    def test_network_error_returns_empty(self):
        import requests as _r
        with patch("waa.discovery.prospector.requests.post", side_effect=_r.RequestException("boom")):
            self.assertEqual(prospector.search_overpass("restauracia", "Bratislava"), [])


class TestProspectPrefersOverpass(unittest.TestCase):

    def test_overpass_results_skip_web_search(self):
        osm_hits = [{"url": "https://a.sk", "title": "A", "snippet": ""},
                    {"url": "https://b.sk", "title": "B", "snippet": ""}]
        with patch("waa.discovery.prospector.search_overpass", return_value=osm_hits) as ov, \
             patch("waa.discovery.prospector.search_google_serp") as serp, \
             patch("waa.discovery.prospector.search_duckduckgo") as ddg, \
             patch("waa.discovery.prospector.quick_qualify",
                   side_effect=lambda url: {"url": url, "qualified": True, "score": 50,
                                            "red_flags": [], "green_flags": [], "name": ""}), \
             patch("waa.discovery.prospector.time.sleep"):
            out = prospector.prospect("restauracia", "Bratislava",
                                      num_results=2, min_score=10)
        ov.assert_called_once()
        # With enough OSM hits, neither web-search provider should be touched.
        serp.assert_not_called()
        ddg.assert_not_called()
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
