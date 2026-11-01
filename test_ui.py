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

    def package_pair(self):
        for uid, name, dep in (("Test.Code", "Better Balance C#", None), ("Test.Pack", "Better Balance", "Test.Code")):
            folder = self.game / "Mods" / "Package" / uid
            folder.mkdir(parents=True)
            manifest = dict(Name=name, UniqueID=uid, Version="1.0.0", Author="Test author")
            if dep:
                manifest["Dependencies"] = [{"UniqueID": dep}]
            core.write_json(folder / "manifest.json", manifest)
        self.window.refresh()
        return next(mod for mod in self.window.mods if mod.uid == "Test.Pack")

    def test_package_confirmation_includes_code_component_by_default(self):
        pack = self.package_pair()
        def confirm(box):
            self.assertTrue(box.checkBox().isChecked())
            self.assertIn("Better Balance C#", box.informativeText())
            self.assertIn("2 个 MOD 组件", box.text())
            return QMessageBox.StandardButton.Yes
        with patch.object(QMessageBox, "exec", confirm), patch.object(self.window, "run_job") as job:
            self.window.change_state(False, [pack])
        folder = job.call_args.args[0](lambda _: None)
        record = core.json.loads((folder / "transaction.json").read_text(encoding="utf-8"))
        self.assertEqual({entry["uid"] for entry in record["entries"]}, {"Test.Pack", "Test.Code"})

    def test_package_confirmation_can_explicitly_disable_only_selected_component(self):
        pack = self.package_pair()
        def confirm(box):
            box.checkBox().setChecked(False)
            self.assertIn("仅调整所选项", box.informativeText())
            self.assertIn("1 个 MOD 组件", box.text())
            self.assertNotIn("Test.Code", box.detailedText())
            return QMessageBox.StandardButton.Yes
        with patch.object(QMessageBox, "exec", confirm), patch.object(self.window, "run_job") as job:
            self.window.change_state(False, [pack])
        job.call_args.args[0](lambda _: None)
        rows = {m.uid: m for m in core.scan(self.game)[0]}
        self.assertTrue(rows["Test.Pack"].disabled)
        self.assertFalse(rows["Test.Code"].disabled)

    def test_package_confirmation_cancel_keeps_both_components(self):
        pack = self.package_pair()
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Cancel), \
             patch.object(self.window, "run_job") as job:
            self.window.change_state(False, [pack])
        job.assert_not_called()
        self.assertFalse(any(m.disabled for m in core.scan(self.game)[0]))

    def test_package_details_show_remaining_enabled_code_for_disabled_pack(self):
        pack = self.package_pair()
        core.ModStatePlan(self.game, [pack], False).execute()
        self.window.refresh()
        row = next(i for i, mod in enumerate(self.window.visible) if mod.uid == "Test.Pack")
        self.window.table.setCurrentCell(row, 1)
        self.window.show_details()
        text = self.window.detail.toPlainText()
        self.assertIn("配套组件", text)
        self.assertIn("Better Balance C#（已启用）", text)

    def test_package_confirmation_reenables_all_disabled_components(self):
        self.package_pair()
        selected = [mod for mod in self.window.mods if mod.uid in {"Test.Pack", "Test.Code"}]
        core.ModStatePlan(self.game, selected, False).execute()
        self.window.refresh()
        pack = next(mod for mod in self.window.mods if mod.uid == "Test.Pack")
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(self.window, "run_job") as job:
            self.window.change_state(True, [pack])
        job.call_args.args[0](lambda _: None)
        self.assertFalse(any(mod.disabled for mod in core.scan(self.game)[0]))

    def test_running_game_blocks_package_prompt_and_file_changes(self):
        pack = self.package_pair()
        with patch("core.game_running", return_value=True), patch.object(self.window, "failed") as failed, \
             patch.object(QMessageBox, "exec") as prompt, patch.object(self.window, "run_job") as job:
            self.window.change_state(False, [pack])
        prompt.assert_not_called()
        job.assert_not_called()
        failed.assert_called_once()

    def group_fixture(self):
        for uid in ("Test.Second", "Test.Third"):
            folder = self.game / "Mods" / uid
            folder.mkdir()
            core.write_json(folder / "manifest.json", dict(Name=uid, UniqueID=uid, Version="1.0.0"))
        self.window.refresh()
        group = self.window.group_store.save("种田", ["Test.Example", "Test.Second"])
        self.window.reload_groups(group["id"])
        self.window.group_changed()
        return group

    def test_visible_select_all_and_invert_do_not_toggle_enabled_flags(self):
        self.group_fixture()
        self.window.select_all.click()
        self.assertEqual(len(self.window.selected_mods()), 2)
        self.window.invert_selection.click()
        self.assertEqual(self.window.selected_mods(), [])
        self.window.invert_selection.click()
        self.assertEqual(len(self.window.selected_mods()), 2)
        self.assertFalse(any(mod.disabled for mod in core.scan(self.game)[0]))

    def test_select_all_respects_group_and_search(self):
        self.group_fixture()
        self.window.search.setText("Second")
        self.window.select_all.click()
        self.assertEqual([mod.uid for mod in self.window.selected_mods()], ["Test.Second"])

    def test_group_filter_composes_with_state_filter_and_search(self):
        self.group_fixture()
        self.assertEqual({mod.uid for mod in self.window.visible}, {"Test.Example", "Test.Second"})
        core.ModStatePlan(self.game, [mod for mod in self.window.mods if mod.uid == "Test.Second"], False).execute()
        self.window.refresh()
        self.window.set_filter("disabled")
        self.assertEqual([mod.uid for mod in self.window.visible], ["Test.Second"])
        self.window.search.setText("Third")
        self.assertEqual(self.window.table.rowCount(), 0)

    def test_group_selection_is_remembered_after_refresh(self):
        group = self.group_fixture()
        self.window.refresh()
        self.assertEqual(self.window.group_combo.currentData(), group["id"])
        saved = core.json.loads(self.window.settings.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["group_selection"][self.window.group_store.key], group["id"])

    def test_switching_group_view_never_moves_mod_files(self):
        self.group_fixture()
        before = {str(mod.path) for mod in core.scan(self.game)[0]}
        self.window.group_combo.setCurrentIndex(0)
        self.assertEqual(self.window.table.rowCount(), 3)
        self.window.group_combo.setCurrentIndex(1)
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual({str(mod.path) for mod in core.scan(self.game)[0]}, before)

    def test_add_and_remove_group_members_only_edits_metadata(self):
        group = self.group_fixture()
        self.window.group_combo.setCurrentIndex(0)
        self.window.select_visible()
        self.window.group_id = group["id"]
        self.window.add_to_group()
        self.assertEqual(len(self.window.current_group()["members"]), 3)
        self.window.search.setText("Third")
        self.window.select_visible()
        self.window.remove_from_group()
        self.assertEqual(set(self.window.current_group()["members"]), {"Test.Example", "Test.Second"})
        self.assertTrue(all(not mod.disabled for mod in core.scan(self.game)[0]))

    def test_group_disable_targets_whole_group_even_when_search_hides_members(self):
        self.group_fixture()
        self.window.search.setText("Second")
        with patch.object(self.window, "change_state") as change:
            self.window.group_disable.click()
        enabled, members = change.call_args.args
        self.assertFalse(enabled)
        self.assertEqual({mod.uid for mod in members}, {"Test.Example", "Test.Second"})

    def test_group_apply_preview_explicitly_disables_outside_and_can_cancel(self):
        self.group_fixture()
        def cancel(box):
            self.assertIn("禁用 1 项", box.text())
            self.assertIn("组外 MOD 将被禁用", box.informativeText())
            self.assertIn("Test.Third", box.detailedText())
            return QMessageBox.StandardButton.Cancel
        with patch.object(QMessageBox, "exec", cancel), patch.object(self.window, "run_job") as job:
            self.window.apply_group()
        job.assert_not_called()
        self.assertTrue(all(not mod.disabled for mod in core.scan(self.game)[0]))

    def test_confirmed_group_apply_uses_one_mixed_transaction(self):
        self.group_fixture()
        second = next(mod for mod in self.window.mods if mod.uid == "Test.Second")
        core.ModStatePlan(self.game, [second], False).execute()
        self.window.refresh()
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(self.window, "run_job") as job:
            self.window.apply_group()
        folder = job.call_args.args[0](lambda _: None)
        journal = core.json.loads((folder / "transaction.json").read_text(encoding="utf-8"))
        self.assertIsNone(journal["enabled"])
        self.assertEqual({entry["enabled"] for entry in journal["entries"]}, {True, False})
        rows = {mod.uid: mod.disabled for mod in core.scan(self.game)[0]}
        self.assertEqual(rows, {"Test.Example": False, "Test.Second": False, "Test.Third": True})

    def test_group_editor_can_save_copy_and_remove_missing_members(self):
        group = self.window.group_store.save("Original", ["Test.Example", "Missing.Mod"])
        dialog = app.GroupEditor(self.window, group)
        self.assertIn("Missing.Mod", dialog.members())
        self.assertTrue(any("未安装" in title for _, title in dialog.rows))
        dialog.set_members(["Test.Example"])
        dialog.name.setText("Copied")
        dialog.save_group(copy=True)
        self.assertNotEqual(dialog.saved_identifier, group["id"])
        self.assertEqual(self.window.group_store.get(dialog.saved_identifier)["members"], ["Test.Example"])
        self.assertEqual(self.window.group_store.get(group["id"])["members"], ["Test.Example", "Missing.Mod"])
        dialog.deleteLater()

    def test_group_editor_duplicate_name_keeps_original_and_shows_reason(self):
        group = self.window.group_store.save("Original", ["Test.Example"])
        self.window.group_store.save("Taken", [])
        dialog = app.GroupEditor(self.window, group)
        dialog.name.setText("Taken")
        with patch.object(QMessageBox, "warning") as warning:
            dialog.save_group()
        warning.assert_called_once()
        self.assertEqual(self.window.group_store.get(group["id"])["name"], "Original")
        dialog.reject()
        dialog.deleteLater()

    def test_copy_group_generates_an_available_name(self):
        group = self.window.group_store.save("Original", ["Test.Example"])
        self.window.group_store.save("Original 副本", [])
        dialog = app.GroupEditor(self.window, group)
        dialog.save_group(copy=True)
        self.assertEqual(self.window.group_store.get(dialog.saved_identifier)["name"], "Original 副本 2")
        dialog.deleteLater()

    def test_delete_group_does_not_disable_its_mods(self):
        group = self.group_fixture()
        dialog = app.GroupEditor(self.window, group)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.delete_group()
        self.assertTrue(dialog.deleted)
        self.assertIsNone(self.window.group_store.get(group["id"]))
        self.assertTrue(all(not mod.disabled for mod in core.scan(self.game)[0]))
        dialog.deleteLater()

    def test_empty_group_confirmation_warns_all_mods_will_be_disabled(self):
        group = self.window.group_store.save("空组", [])
        self.window.reload_groups(group["id"])
        def cancel(box):
            self.assertIn("空编组，将禁用所有 MOD", box.informativeText())
            return QMessageBox.StandardButton.Cancel
        with patch.object(QMessageBox, "exec", cancel), patch.object(self.window, "run_job") as job:
            self.window.apply_group()
        job.assert_not_called()

    def test_busy_prevents_group_apply_and_membership_changes(self):
        group = self.group_fixture()
        self.window.busy = True
        with patch.object(app, "ModGroupPlan") as plan:
            self.window.apply_group()
            self.window.remove_from_group()
        plan.assert_not_called()
        self.assertEqual(self.window.group_store.get(group["id"])["members"], ["Test.Example", "Test.Second"])
        self.window.busy = False

    def test_editor_add_required_dependencies_to_draft_only(self):
        pack = self.package_pair()
        dialog = app.GroupEditor(self.window, members=[pack.uid])
        dialog.add_dependencies()
        self.assertEqual(set(dialog.members()), {"Test.Pack", "Test.Code"})
        self.assertEqual(self.window.group_store.groups, [])
        self.assertFalse(any(mod.disabled for mod in core.scan(self.game)[0]))
        dialog.reject()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main(verbosity=2)
