"""Reproducible collaboration environment-lock tests."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from freecad_collaboration.environment import (
    build_environment_lock,
    environment_lock_id,
    read_environment_lock,
    write_environment_lock,
)


PACKAGE_XML = """<?xml version="1.0"?>
<package><name>Test Addon</name><version>1.2.3</version></package>
"""


class EnvironmentLockTests(unittest.TestCase):
    def _addon(self, parent, content="VALUE = 1\n"):
        addon = Path(parent) / "same-addon-name"
        addon.mkdir()
        (addon / "package.xml").write_text(PACKAGE_XML, encoding="utf-8")
        (addon / "module.py").write_text(content, encoding="utf-8")
        (addon / "ignored.txt").write_text("machine-local note", encoding="utf-8")
        return addon

    def test_lock_is_path_independent_and_changes_with_code(self):
        with tempfile.TemporaryDirectory() as first_parent, tempfile.TemporaryDirectory() as second_parent:
            first = self._addon(first_parent)
            second = self._addon(second_parent)
            first_lock = build_environment_lock([first])
            second_lock = build_environment_lock([second])
            self.assertEqual(first_lock, second_lock)
            self.assertEqual(environment_lock_id(first_lock), environment_lock_id(second_lock))

            (second / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
            changed = build_environment_lock([second])
            self.assertNotEqual(environment_lock_id(first_lock), environment_lock_id(changed))

    def test_lockfile_round_trip(self):
        with tempfile.TemporaryDirectory() as parent:
            addon = self._addon(parent)
            lock = build_environment_lock([addon])
            output = Path(parent) / "collaboration-environment.lock.json"
            identity = write_environment_lock(output, lock)
            self.assertEqual(read_environment_lock(output), lock)
            self.assertEqual(environment_lock_id(lock), identity)


if __name__ == "__main__":
    unittest.main()
