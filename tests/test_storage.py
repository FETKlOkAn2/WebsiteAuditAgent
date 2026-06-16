"""
Tests for storage.py — the shared domain_of helper and JsonStore.

These utilities are now used by both audit_agent and replies_monitor, so
they get their own focused coverage rather than only being exercised
indirectly through the wrappers.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from waa.core.storage import domain_of, JsonStore  # noqa: E402


class TestDomainOf(unittest.TestCase):

    def test_strips_scheme_and_www_and_lowercases(self):
        self.assertEqual(domain_of("https://www.Example.com/path"), "example.com")

    def test_bare_domain(self):
        self.assertEqual(domain_of("example.com"), "example.com")

    def test_missing_scheme_with_path(self):
        self.assertEqual(domain_of("example.com/contact"), "example.com")

    def test_subdomain_preserved(self):
        self.assertEqual(domain_of("https://shop.example.co.uk/x"), "shop.example.co.uk")

    def test_empty(self):
        self.assertEqual(domain_of(""), "")

    def test_only_strips_www_as_prefix_not_chars(self):
        # Regression: the old `.lstrip("www.")` would mangle these because
        # lstrip removes any leading w/./ characters, not the literal prefix.
        self.assertEqual(domain_of("wine.sk"), "wine.sk")
        self.assertEqual(domain_of("https://wwwf.example.com"), "wwwf.example.com")


class TestJsonStore(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jsonstore_")
        self.path = os.path.join(self.tmpdir, "data.json")

    def _store(self, **kw):
        return JsonStore(self.path, lambda: {"items": []}, **kw)

    def test_load_missing_returns_default(self):
        self.assertEqual(self._store().load(), {"items": []})

    def test_roundtrip(self):
        store = self._store()
        store.save({"items": [1, 2, 3]})
        self.assertEqual(store.load(), {"items": [1, 2, 3]})

    def test_default_factory_returns_fresh_object_each_call(self):
        # Two missing-file loads must not share a mutable default.
        store = self._store()
        a = store.load()
        a["items"].append("x")
        b = store.load()
        self.assertEqual(b["items"], [])

    def test_save_creates_parent_dir(self):
        nested = os.path.join(self.tmpdir, "a", "b", "c.json")
        store = JsonStore(nested, lambda: {})
        store.save({"ok": True})
        self.assertTrue(os.path.exists(nested))

    def test_save_is_utf8_readable(self):
        # ensure_ascii=False — Slovak text should be human-readable in the file
        store = self._store()
        store.save({"items": ["Žilina", "Košice"]})
        raw = open(self.path, encoding="utf-8").read()
        self.assertIn("Žilina", raw)

    def test_corrupt_raises_when_not_tolerant(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        with self.assertRaises(json.JSONDecodeError):
            self._store(tolerate_corrupt=False).load()

    def test_corrupt_resets_when_tolerant(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        self.assertEqual(self._store(tolerate_corrupt=True).load(), {"items": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
