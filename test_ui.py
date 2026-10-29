"""Small UI checks against temporary fixture data, without network or live Mods changes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import core
import app


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        cls.qt.setStyleSheet(app.STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.game = root / "game"
        folder = self.game / "Mods" / "TestMod"
        folder.mkdir(parents=True)
        (self.game / "StardewModdingAPI.exe").write_bytes(b"fixture")
        core.write_json(folder / "manifest.json", {"Name": "测试 MOD", "UniqueID": "Test.Example", "Version": "1.0.0",
                                                  "UpdateKeys": ["Nexus:123"]})
        self.patches = [patch("core.installed_versions", return_value=("4.5.2", "1.6.15")),
                        patch("app.installed_versions", return_value=("4.5.2", "1.6.15")),
                        patch("app.DATA", root / "data")]
        for p in self.patches:
            p.start()
        settings = core.Settings(root / "data")
        settings.values.update(game=str(self.game), auto_check=False)
        self.window = app.Window(settings)

    def tearDown(self):
        self.window.timer.stop()
        self.window.close()
        self.window.deleteLater()
        self.qt.processEvents()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_initial_scan_selection_and_details(self):
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(self.window.selected().uid, "Test.Example")
        self.assertIn("测试 MOD", self.window.detail.toPlainText())

    def test_search_and_filters(self):
        self.window.search.setText("does not exist")
        self.assertEqual(self.window.table.rowCount(), 0)
        self.window.search.clear()
        self.assertEqual(self.window.table.rowCount(), 1)
        self.window.set_filter("updates")
        self.assertEqual(self.window.table.rowCount(), 0)
        self.window.set_filter("all")
        self.assertEqual(self.window.table.rowCount(), 1)

    def test_checked_results_update_visible_version(self):
        cache = {"schema": core.CACHE_SCHEMA, "game": str(self.game), "time": 1, "versions": {"test.example": "1.0.0"},
                 "keys": {"test.example": ["Nexus:123"]},
                 "results": {"test.example": {"id": "Test.Example", "suggestedUpdate": {
                     "version": "2.0.0", "url": "https://www.nexusmods.com/stardewvalley/mods/123"}}}}
        self.window.checked(cache, False)
        self.assertEqual(self.window.counts[1].text(), "1")
        self.assertEqual(self.window.table.item(0, 2).text(), "2.0.0")
        self.assertIn("2.0.0", self.window.detail.toPlainText())

    def test_settings_lists_detected_mods_path(self):
        found = [{"game": str(self.game), "mods": str(self.game / "Mods"), "sources": ["Steam 库"], "priority": 1}]
        with patch("app.discover_games", return_value=found):
            dialog = app.SettingsDialog(self.window)
        self.assertEqual(dialog.detected_paths.count(), 1)
        self.assertEqual(dialog.path.text(), str(self.game))
        self.assertIn(str(self.game / "Mods"), dialog.detect_hint.text())
        dialog.reject()

    def test_partial_source_warning_is_not_failed_but_still_in_details(self):
        mod = self.window.mods[0]
        mod.update = core.prepare_update_result(mod, {"id": mod.uid, "errors": ["Nexus page unavailable"],
            "metadata": {"main": {"version": "1.0.0", "url": "https://www.moddrop.com/stardew-valley/mods/1"}}})
        self.window.populate()
        self.assertEqual(self.window.table.item(0, 3).text(), "无推荐更新")
        self.assertEqual(self.window.table.item(0, 4).text(), "ModDrop")
        self.assertIn("Nexus page unavailable", self.window.detail.toPlainText())


if __name__ == "__main__":
    unittest.main(verbosity=2)
