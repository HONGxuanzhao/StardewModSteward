"""Persistent groups and transactional loadouts, using temporary game fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.game = self.root / "game"
        self.make_game(self.game)
        self.data = self.root / "data"
        self.patches = [patch("core.game_running", return_value=False),
                        patch("core.installed_versions", return_value=("4.5.2", "1.6.15"))]
        for item in self.patches:
            item.start()
        self.store = core.ModGroups(self.game, self.data)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def make_game(self, game):
        (game / "Mods").mkdir(parents=True)
        (game / "StardewModdingAPI.exe").write_bytes(b"fixture")

    def install(self, uid, folder=None, **extra):
        path = self.game / "Mods" / (folder or uid)
        path.mkdir(parents=True)
        core.write_json(path / "manifest.json", dict(Name=uid, UniqueID=uid, Version="1.0.0", **extra))
        (path / "config.json").write_bytes(b'{"mine":true}')
        (path / "payload.dll").write_bytes(b"original data")
        return path

    def rows(self):
        mods, errors = core.scan(self.game)
        self.assertFalse(errors)
        return mods

    def state(self):
        return {mod.uid: not mod.disabled for mod in self.rows()}

    def payloads(self):
        return {mod.uid: {p.relative_to(mod.path).as_posix(): p.read_bytes()
                         for p in mod.path.rglob("*") if p.is_file() and p.name != ".steward-disabled.json"}
                for mod in self.rows()}

    def mixed_fixture(self):
        self.install("Test.A")
        self.install("Test.B")
        core.ModStatePlan(self.game, [m for m in self.rows() if m.uid == "Test.B"], False).execute()

    def test_create_rename_copy_delete_persist_without_changing_mods(self):
        self.install("Test.A")
        original = self.payloads()
        group = self.store.save("种田", ["Test.A", "test.a"])
        self.assertEqual(group["members"], ["Test.A"])
        group = self.store.save("联机", group["members"], group["id"])
        duplicate = self.store.save("联机副本", group["members"])
        self.store.delete(group["id"])
        reopened = core.ModGroups(self.game, self.data)
        self.assertEqual(reopened.groups, [duplicate])
        self.assertEqual(self.payloads(), original)
        self.assertTrue(self.state()["Test.A"])

    def test_invalid_and_duplicate_names_rejected_without_file_change(self):
        self.store.save("Farm", [])
        original = self.store.path.read_bytes()
        for name in ("", "  ", "x" * 61, None, "farm"):
            with self.assertRaises(core.ModError):
                self.store.save(name, [])
            self.assertEqual(self.store.path.read_bytes(), original)

    def test_members_are_validated_and_deduplicated_case_insensitively(self):
        group = self.store.save("Test", ["Test.A", "test.a", " Test.B "])
        self.assertEqual(group["members"], ["Test.A", "Test.B"])
        for members in ("Test.A", [None], [""], ["x" * 257]):
            with self.assertRaises(core.ModError):
                self.store.save("Invalid", members)

    def test_other_installations_preserved_even_with_two_open_stores(self):
        other = self.root / "other"
        self.make_game(other)
        second = core.ModGroups(other, self.data)
        first_group = self.store.save("First", ["Test.A"])
        second_group = second.save("Second", ["Test.B"])
        self.assertEqual(core.ModGroups(self.game, self.data).groups, [first_group])
        self.assertEqual(core.ModGroups(other, self.data).groups, [second_group])

    def test_previous_document_kept_before_edit(self):
        group = self.store.save("First", [])
        previous = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.store.save("Renamed", [], group["id"])
        self.assertEqual(json.loads((self.data / "groups.previous.json").read_text(encoding="utf-8")), previous)

    def test_corrupt_or_future_group_file_never_overwritten(self):
        self.data.mkdir()
        path = self.data / "groups.json"
        for content in (b"{broken", b'{"schema":999,"games":{}}', b'[]'):
            path.write_bytes(content)
            with self.assertRaisesRegex(core.ModError, "原文件未改动"):
                core.ModGroups(self.game, self.data)
            self.assertEqual(path.read_bytes(), content)

    def test_failed_metadata_write_rolls_back_in_memory_and_on_disk(self):
        group = self.store.save("Original", ["Test.A"])
        original = self.store.path.read_bytes()
        with patch("core.write_json", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.save("Changed", [], group["id"])
        self.assertEqual(self.store.groups, [group])
        self.assertEqual(self.store.path.read_bytes(), original)

    def test_uninstalled_members_remain_in_saved_group(self):
        group = self.store.save("Missing", ["Not.Installed"])
        self.assertEqual(core.ModGroups(self.game, self.data).get(group["id"])["members"], ["Not.Installed"])
        with self.assertRaisesRegex(core.ModError, "未安装"):
            core.ModGroupPlan(self.game, group["members"])

    def test_group_members_survive_moves_and_version_updates(self):
        folder = self.install("Test.A")
        group = self.store.save("Farm", ["Test.A"])
        core.ModStatePlan(self.game, self.rows(), False).execute()
        mod = self.rows()[0]
        manifest = dict(mod.manifest, Version="2.0.0")
        core.write_json(mod.path / "manifest.json", manifest)
        core.ModGroupPlan(self.game, group["members"]).execute()
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.rows()[0].version, "2.0.0")

    def test_mixed_apply_and_undo_preserve_bytes_and_paths(self):
        self.mixed_fixture()
        original, state = self.payloads(), self.state()
        plan = core.ModGroupPlan(self.game, ["Test.B"])
        self.assertEqual({entry["enabled"] for entry in plan.items}, {True, False})
        folder = plan.execute()
        self.assertEqual(self.state(), {"Test.A": False, "Test.B": True})
        self.assertEqual(self.payloads(), original)
        core.restore_backup(self.game, folder)
        self.assertEqual(self.state(), state)
        self.assertEqual(self.payloads(), original)

    def test_required_dependencies_and_content_pack_host_added_optional_ignored(self):
        self.install("Test.Host")
        self.install("Test.Code")
        self.install("Test.Optional")
        self.install("Test.Pack", ContentPackFor={"UniqueID": "Test.Host"},
                     Dependencies=[{"UniqueID": "Test.Code"}, {"UniqueID": "Test.Optional", "IsRequired": False}])
        plan = core.ModGroupPlan(self.game, ["test.pack"])
        self.assertEqual({mod.uid for mod in plan.dependencies}, {"Test.Host", "Test.Code"})
        plan.execute()
        self.assertEqual(self.state(), {"Test.Code": True, "Test.Host": True, "Test.Optional": False, "Test.Pack": True})

    def test_dependency_cycle_resolves_without_hanging(self):
        self.install("Test.A", Dependencies=[{"UniqueID": "Test.B"}])
        self.install("Test.B", Dependencies=[{"UniqueID": "Test.A"}])
        self.assertEqual({mod.uid for mod in core.resolve_group_members(["Test.A"], self.rows())}, {"Test.A", "Test.B"})

    def test_missing_dependency_blocks_before_any_moves(self):
        self.install("Test.A", Dependencies=[{"UniqueID": "Missing.Framework"}])
        original = self.payloads()
        with self.assertRaisesRegex(core.ModError, "Missing.Framework"):
            core.ModGroupPlan(self.game, ["Test.A"])
        self.assertEqual(self.payloads(), original)
        self.assertTrue(self.state()["Test.A"])

    def test_duplicate_desired_id_is_not_guessed(self):
        self.install("Test.Dup", "one")
        self.install("Test.Dup", "two")
        with self.assertRaisesRegex(core.ModError, "重复 UniqueID"):
            core.ModGroupPlan(self.game, ["Test.Dup"])

    def test_duplicate_excluded_id_can_be_disabled_together(self):
        self.install("Test.Dup", "one")
        self.install("Test.Dup", "two")
        self.install("Test.Keep")
        core.ModGroupPlan(self.game, ["Test.Keep"]).execute()
        self.assertTrue(all(m.disabled for m in self.rows() if m.uid == "Test.Dup"))

    def test_mixed_move_failure_rolls_every_component_back(self):
        self.mixed_fixture()
        original, state = self.payloads(), self.state()
        original_replace = core.os.replace
        moves = []
        def fail_second_move(source, destination):
            if Path(source).is_dir():
                moves.append(str(source))
                if len(moves) == 2:
                    raise OSError("injected second move failure")
            return original_replace(source, destination)
        with patch("core.os.replace", side_effect=fail_second_move):
            with self.assertRaises(OSError):
                core.ModGroupPlan(self.game, ["Test.B"]).execute()
        self.assertEqual(self.state(), state)
        self.assertEqual(self.payloads(), original)
        self.assertEqual(core.backup_list(self.game)[0]["state"], "rolled_back")

    def test_interrupted_mixed_transaction_recovers_move_before_marker(self):
        self.mixed_fixture()
        original, state = self.payloads(), self.state()
        plan = core.ModGroupPlan(self.game, ["Test.B"])
        folder = self.game / ".stardew-mod-steward" / "backups" / "interrupted"
        folder.mkdir()
        journal = dict(kind="mod_state", action="切换方案", state="prepared", game=str(self.game),
                       time=core.time.time(), enabled=None, entries=plan.items)
        core.write_json(folder / "transaction.json", journal)
        entry = plan.items[0]
        source, target = core._state_paths(self.game, entry)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        core.restore_backup(self.game, folder)
        self.assertEqual(self.state(), state)
        self.assertEqual(self.payloads(), original)

    def test_running_game_blocks_both_prepare_and_execute(self):
        self.install("Test.A")
        plan = core.ModGroupPlan(self.game, [])
        with patch("core.game_running", return_value=True):
            with self.assertRaises(core.ModError):
                core.ModGroupPlan(self.game, [])
            with self.assertRaises(core.ModError):
                plan.execute()
        self.assertTrue(self.state()["Test.A"])

    def test_inventory_change_after_preview_requires_new_confirmation(self):
        self.install("Test.A")
        plan = core.ModGroupPlan(self.game, [])
        self.install("Test.New")
        with self.assertRaisesRegex(core.ModError, "列表在预览后发生变化"):
            plan.execute()
        self.assertTrue(all(self.state().values()))

    def test_manifest_dependency_change_after_preview_requires_new_confirmation(self):
        self.install("Test.A")
        path = self.install("Test.B")
        plan = core.ModGroupPlan(self.game, [])
        core.write_json(path / "manifest.json", dict(Name="Test.B", UniqueID="Test.B", Version="1.0.0", Description="changed"))
        with self.assertRaisesRegex(core.ModError, "列表在预览后发生变化"):
            plan.execute()
        self.assertTrue(all(self.state().values()))

    def test_empty_group_disables_all_and_can_be_undone(self):
        self.install("Test.A")
        self.install("Test.B")
        folder = core.ModGroupPlan(self.game, []).execute()
        self.assertFalse(any(self.state().values()))
        core.restore_backup(self.game, folder)
        self.assertTrue(all(self.state().values()))

    def test_same_mod_cannot_have_conflicting_desired_states(self):
        self.install("Test.A")
        mod = self.rows()[0]
        with self.assertRaisesRegex(core.ModError, "同时启用和禁用"):
            core.ModStatePlan(self.game, [(mod, True), (mod, False)], None)
        self.assertTrue(self.state()["Test.A"])

    def test_resolved_dependency_version_is_checked(self):
        self.install("Test.Host")
        self.install("Test.Pack", Dependencies=[{"UniqueID": "Test.Host", "MinimumVersion": "9.0.0"}])
        core.ModStatePlan(self.game, self.rows(), False).execute()
        with self.assertRaisesRegex(core.ModError, "9.0.0"):
            core.ModGroupPlan(self.game, ["Test.Pack"])

    def test_already_enabled_group_still_checks_dependency_versions(self):
        self.install("Test.Host")
        self.install("Test.Pack", Dependencies=[{"UniqueID": "Test.Host", "MinimumVersion": "9.0.0"}])
        self.install("Test.Outside")
        with self.assertRaisesRegex(core.ModError, "9.0.0"):
            core.ModGroupPlan(self.game, ["Test.Pack"])
        self.assertTrue(all(self.state().values()))

    def test_already_enabled_group_checks_minimum_smapi_version(self):
        self.install("Test.Future", MinimumApiVersion="99.0.0")
        with self.assertRaisesRegex(core.ModError, "SMAPI 99.0.0"):
            core.ModGroupPlan(self.game, ["Test.Future"])

    def test_backup_display_uses_time_even_for_same_second_folder_names(self):
        root = self.game / ".stardew-mod-steward" / "backups"
        core.write_json(root / "20261101-100000-zzzz" / "transaction.json", {"time": 1})
        core.write_json(root / "20261101-100000-aaaa" / "transaction.json", {"time": 2})
        self.assertEqual([row["time"] for row in core.backup_list(self.game)], [2, 1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
