"""Tests for organization-scoped local file storage."""

import tempfile

from django.test import SimpleTestCase

from common.storage import LocalFileAdapter, UnsafeStorageKey


class LocalStorageTests(SimpleTestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalFileAdapter(tmp)
            key = adapter.build_key("org-1", "imports", "a.bin")
            adapter.save_bytes(key, b"bytes-here")
            self.assertTrue(adapter.exists(key))
            self.assertEqual(adapter.read_bytes(key), b"bytes-here")

    def test_keys_are_scoped_by_organization(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalFileAdapter(tmp)
            key = adapter.build_key("org-1", "x.bin")
            self.assertTrue(key.startswith("org-1/"))

    def test_rejects_unsafe_segments(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalFileAdapter(tmp)
            bad_segments = ["", ".", "..", "../x", "a/b", "a\\b", "/abs", "a\x00b"]
            for bad in bad_segments:
                with self.subTest(segment=bad):
                    with self.assertRaises(UnsafeStorageKey):
                        adapter.build_key(bad, "x.bin")
                    with self.assertRaises(UnsafeStorageKey):
                        adapter.build_key("org-1", bad)

    def test_resolve_refuses_traversal_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalFileAdapter(tmp)
            for bad_key in ["../escape.bin", "/abs.bin", "", "org-1/../../escape.bin"]:
                with self.subTest(key=bad_key):
                    with self.assertRaises(UnsafeStorageKey):
                        adapter.save_bytes(bad_key, b"x")
                    with self.assertRaises(UnsafeStorageKey):
                        adapter.exists(bad_key)

    def test_missing_key_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = LocalFileAdapter(tmp)
            key = adapter.build_key("org-1", "missing.bin")
            self.assertFalse(adapter.exists(key))
            with self.assertRaises(FileNotFoundError):
                adapter.read_bytes(key)
