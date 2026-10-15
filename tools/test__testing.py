"""Tests for tools/_testing.py helpers (home_path fixture builder).

Run: py -3.13 -m pytest -q tools/test__testing.py
Expected strings are assembled from parts so this file holds no literal home path.
"""

import unittest

import privacy_guard
from _testing import HOME_STYLES, home_path

USERS = "Us" + "ers"


class HomePathTests(unittest.TestCase):
    def test_default_is_windows_backslash_form_for_alice(self):
        self.assertEqual(home_path(), "C:\\" + USERS + "\\alice")

    def test_every_style(self):
        want = {
            "win": "C:\\" + USERS + "\\bob",
            "posix": "C:/" + USERS + "/bob",
            "gitbash": "/c/" + USERS + "/bob",
            "transcript": "C--" + USERS + "-bob",
        }
        self.assertEqual(set(want), set(HOME_STYLES))
        for style, expected in want.items():
            with self.subTest(style=style):
                self.assertEqual(home_path("bob", style), expected)

    def test_drive_letter(self):
        self.assertEqual(home_path("bob", "win", drive="D"), "D:\\" + USERS + "\\bob")
        self.assertEqual(home_path("bob", "gitbash", drive="D"), "/d/" + USERS + "/bob")
        self.assertEqual(
            home_path("bob", "transcript", drive="e"), "e--" + USERS + "-bob"
        )

    def test_user_inserted_as_given(self):
        self.assertEqual(
            home_path("Alice Smith", "posix"), "C:/" + USERS + "/Alice Smith"
        )
        self.assertEqual(home_path("<you>", "transcript"), "C--" + USERS + "-<you>")

    def test_built_paths_are_flagged_by_the_privacy_guard(self):
        guard = privacy_guard.Guard([], [], [])
        for style in HOME_STYLES:
            with self.subTest(style=style):
                line = home_path("alice", style) + "-x/y"
                reasons = [h.reason for h in guard.scan(line)]
                self.assertTrue(any("user-home path" in r for r in reasons), reasons)

    def test_bad_arguments_raise(self):
        for kwargs in (
            {"style": "mac"},
            {"drive": "CD"},
            {"drive": ""},
            {"drive": "1"},
            {"user": ""},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                home_path(**kwargs)


if __name__ == "__main__":
    unittest.main()
