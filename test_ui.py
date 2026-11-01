"""Small UI checks against temporary fixture data, without network or live Mods changes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QItemSelectionModel
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
                        patch("core.game_running", return_value=False),
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
        self.assertEqual(self.window.table.item(0, 3).text(), "2.0.0")
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
        self.assertEqual(self.window.table.item(0, 4).text(), "无推荐更新")
        self.assertEqual(self.window.table.item(0, 5).text(), "ModDrop")
        self.assertIn("Nexus page unavailable", self.window.detail.toPlainText())

    def test_enabled_checkbox_and_explicit_buttons(self):
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.assertTrue(self.window.disable_selected.isEnabled())
        self.assertFalse(self.window.enable_selected.isEnabled())
        self.assertIn("已启用", self.window.detail.toPlainText())
        self.window.set_filter("enabled")
        self.assertEqual(self.window.table.rowCount(), 1)

    def test_checkbox_dispatches_only_clicked_mod_and_reverts_until_commit(self):
        mod = self.window.mods[0]
        with patch.object(self.window, "change_state") as change:
            self.window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        change.assert_called_once_with(False, [mod])
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)

    def test_cancelled_disable_keeps_files_and_checkbox(self):
        original = self.window.mods[0].path
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Cancel), \
             patch.object(self.window, "run_job") as job:
            self.window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        job.assert_not_called()
        self.assertTrue(original.exists())
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)

    def test_disabled_filter_and_reenable_button(self):
        core.ModStatePlan(self.game, self.window.mods, False).execute()
        self.window.refresh()
        self.window.set_filter("disabled")
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(self.window.table.item(0, 4).text(), "已禁用")
        self.assertTrue(self.window.enable_selected.isEnabled())
        self.assertFalse(self.window.disable_selected.isEnabled())
        self.window.set_filter("enabled")
        self.assertEqual(self.window.table.rowCount(), 0)

    def test_multiple_selection_is_preserved_by_search_repopulation(self):
        extra = self.game / "Mods" / "Second"
        extra.mkdir()
        core.write_json(extra / "manifest.json", {"Name": "Second", "UniqueID": "Test.Second", "Version": "1.0.0"})
        self.window.refresh()
        self.window.table.selectAll()
        self.assertEqual(len(self.window.selected_mods()), 2)
        self.window.populate()
        self.assertEqual(len(self.window.selected_mods()), 2)
        self.assertIn("已选 2 项", self.window.selection_label.text())

    def test_confirmed_batch_uses_state_plan_for_all_selected_mods(self):
        extra = self.game / "Mods" / "Second"
        extra.mkdir()
        core.write_json(extra / "manifest.json", {"Name": "Second", "UniqueID": "Test.Second", "Version": "1.0.0"})
        self.window.refresh()
        self.window.table.selectAll()
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(self.window, "run_job") as job:
            self.window.change_state(False)
        task = job.call_args.args[0]
        folder = task(lambda _: None)
        record = core.json.loads((folder / "transaction.json").read_text(encoding="utf-8"))
        self.assertEqual(len(record["entries"]), 2)
        self.assertTrue(all(m.disabled for m in core.scan(self.game)[0]))

    def test_busy_worker_prevents_checkbox_mutation(self):
        self.window.busy = True
        with patch.object(self.window, "change_state") as change:
            self.window.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        change.assert_not_called()
        self.assertEqual(self.window.table.item(0, 0).checkState(), Qt.CheckState.Checked)
        self.window.busy = False


if __name__ == "__main__":
    unittest.main(verbosity=2)
