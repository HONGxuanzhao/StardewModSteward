"""Enable/disable regression tests; all changes use temporary fake game directories."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core


class ModStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.game = Path(self.temp.name) / "game"
        (self.game / "Mods").mkdir(parents=True)
        (self.game / "StardewModdingAPI.exe").write_bytes(b"fixture")
        self.patches = [patch("core.game_running", return_value=False),
                        patch("core.installed_versions", return_value=("4.5.2", "1.6.15"))]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def install(self, uid, folder=None, version="1.0.0", **extra):
        root = self.game / "Mods" / (folder or uid)
        root.mkdir(parents=True)
        core.write_json(root / "manifest.json", dict(Name=uid, UniqueID=uid, Version=version, **extra))
        (root / "payload.dll").write_bytes(b"binary content\0")
        (root / "config.json").write_bytes(b'{"setting":"mine"}')
        return root

    def rows(self):
        mods, errors = core.scan(self.game)
        self.assertFalse(errors)
        return mods

    def row(self, uid):
        return next(m for m in self.rows() if m.uid == uid)

    def dependency_pair(self):
        self.install("Test.Framework")
        self.install("Test.Content", ContentPackFor={"UniqueID": "Test.Framework"})

    def package_pair(self):
        self.install("Test.Code", "Package/Code", Author="Test author")
        self.install("Test.Pack", "Package/Pack", Author="Test author",
                     Dependencies=[{"UniqueID": "Test.Code"}])

    def test_package_companions_require_same_container_author_and_dependency(self):
        self.package_pair()
        self.install("Test.Unrelated", "Package/Unrelated", Author="Test author")
        self.install("Test.External", Author="Test author", Dependencies=[{"UniqueID": "Test.Code"}])
        rows = self.rows()
        self.assertEqual([m.uid for m in core.state_companions(self.game, [self.row("Test.Pack")], rows)], ["Test.Code"])
        self.assertEqual([m.uid for m in core.state_companions(self.game, [self.row("Test.Code")], rows)], ["Test.Pack"])

    def test_package_companions_find_disabled_original_path_in_both_directions(self):
        self.package_pair()
        core.ModStatePlan(self.game, [self.row("Test.Pack")], False).execute()
        self.assertEqual([m.uid for m in core.state_companions(self.game, [self.row("Test.Pack")], self.rows())], ["Test.Code"])
        core.ModStatePlan(self.game, [self.row("Test.Code")], False).execute()
        self.assertEqual([m.uid for m in core.state_companions(self.game, [self.row("Test.Code")], self.rows())], ["Test.Pack"])

    def test_package_companions_do_not_include_shared_framework_or_other_author(self):
        self.package_pair()
        self.install("Test.Framework", Author="Test author")
        self.install("Test.Patch", "Package/Patch", Author="Someone else",
                     Dependencies=[{"UniqueID": "Test.Code"}])
        pack = self.row("Test.Pack")
        manifest = dict(pack.manifest, ContentPackFor={"UniqueID": "Test.Framework"})
        core.write_json(pack.path / "manifest.json", manifest)
        self.assertEqual([m.uid for m in core.state_companions(self.game, [self.row("Test.Pack")], self.rows())], ["Test.Code"])

    def test_package_companions_do_not_group_top_level_mods(self):
        self.install("Test.Code", Author="Test author")
        self.install("Test.Pack", Author="Test author", Dependencies=[{"UniqueID": "Test.Code"}])
        self.assertEqual(core.state_companions(self.game, [self.row("Test.Pack")], self.rows()), [])

    def test_package_companions_follow_required_chain_and_exclude_selected(self):
        self.package_pair()
        self.install("Test.Third", "Package/Sub/Third", Author="Test author",
                     Dependencies=[{"UniqueID": "Test.Pack"}])
        selected = [self.row("Test.Code"), self.row("Test.Pack")]
        self.assertEqual([m.uid for m in core.state_companions(self.game, selected, self.rows())], ["Test.Third"])

    def test_package_companions_ignore_optional_dependency(self):
        self.package_pair()
        self.install("Test.Optional", "Package/Optional", Author="Test author",
                     Dependencies=[{"UniqueID": "Test.Code", "IsRequired": False}])
        self.assertEqual(core.state_companions(self.game, [self.row("Test.Optional")], self.rows()), [])

    def test_package_companions_skip_invalid_disabled_path_record(self):
        self.package_pair()
        core.ModStatePlan(self.game, [self.row("Test.Pack")], False).execute()
        pack = self.row("Test.Pack")
        for record in ({"relative": "../../outside/Pack"}, {"relative": None}, {"other": 1}):
            core.write_json(pack.path / ".steward-disabled.json", record)
            self.assertEqual(core.state_companions(self.game, [pack], self.rows()), [])

    def test_single_disable_enable_preserves_every_byte_and_original_path(self):
        original = self.install("Test.A", folder="Folder with spaces")
        before = {p.name: p.read_bytes() for p in original.iterdir()}
        core.ModStatePlan(self.game, self.rows(), False).execute()
        disabled = self.row("Test.A")
        self.assertTrue(disabled.disabled)
        self.assertEqual(disabled.status, "已禁用")
        self.assertFalse(original.exists())
        self.assertEqual((disabled.path / "config.json").read_bytes(), before["config.json"])
        core.ModStatePlan(self.game, [disabled], True).execute()
        self.assertEqual(self.row("Test.A").path, original)
        self.assertEqual({p.name: p.read_bytes() for p in original.iterdir()}, before)

    def test_backend_blocks_disabling_required_framework(self):
        self.dependency_pair()
        with self.assertRaisesRegex(core.ModError, "需要启用前置"):
            core.ModStatePlan(self.game, [self.row("Test.Framework")], False)
        self.assertTrue(all(not m.disabled for m in self.rows()))
        self.assertFalse(core.backup_list(self.game))

    def test_batch_can_disable_and_enable_dependent_mods_in_any_order(self):
        self.dependency_pair()
        core.ModStatePlan(self.game, self.rows(), False).execute()
        self.assertTrue(all(m.disabled for m in self.rows()))
        # Content sorts before Framework, but future-state validation permits the complete group.
        core.ModStatePlan(self.game, self.rows(), True).execute()
        self.assertTrue(all(not m.disabled for m in self.rows()))

    def test_enabling_dependent_without_framework_is_blocked(self):
        self.dependency_pair()
        core.ModStatePlan(self.game, self.rows(), False).execute()
        with self.assertRaisesRegex(core.ModError, "需要启用前置"):
            core.ModStatePlan(self.game, [self.row("Test.Content")], True)
        self.assertTrue(all(m.disabled for m in self.rows()))

    def test_optional_dependency_does_not_prevent_disable(self):
        self.install("Test.Framework")
        self.install("Test.Other", Dependencies=[{"UniqueID": "Test.Framework", "IsRequired": False}])
        core.ModStatePlan(self.game, [self.row("Test.Framework")], False).execute()
        self.assertFalse(self.row("Test.Other").disabled)

    def test_content_pack_host_is_always_required(self):
        self.install("Test.Framework")
        self.install("Test.Content", ContentPackFor={"UniqueID": "Test.Framework", "IsRequired": False})
        with self.assertRaises(core.ModError):
            core.ModStatePlan(self.game, [self.row("Test.Framework")], False)

    def test_enable_checks_required_minimum_version(self):
        self.install("Test.Framework", version="1.0.0")
        self.install("Test.Content", Dependencies=[{"UniqueID": "Test.Framework", "MinimumVersion": "2.0.0"}])
        core.ModStatePlan(self.game, [self.row("Test.Content")], False).execute()
        with self.assertRaisesRegex(core.ModError, "需要版本"):
            core.ModStatePlan(self.game, [self.row("Test.Content")], True)

    def test_disable_does_not_block_on_unrelated_existing_missing_dependency(self):
        self.install("Test.Independent")
        self.install("Test.Broken", Dependencies=[{"UniqueID": "Missing.Example"}])
        core.ModStatePlan(self.game, [self.row("Test.Independent")], False).execute()
        self.assertTrue(self.row("Test.Independent").disabled)

    def test_disabling_one_duplicate_is_allowed_but_reenable_is_blocked(self):
        self.install("Test.Duplicate", folder="One")
        self.install("Test.Duplicate", folder="Two")
        row = next(m for m in self.rows() if m.path.name == "One")
        core.ModStatePlan(self.game, [row], False).execute()
        disabled = next(m for m in self.rows() if m.disabled)
        with self.assertRaisesRegex(core.ModError, "相同 UniqueID"):
            core.ModStatePlan(self.game, [disabled], True)

    def test_batch_failure_restores_all_original_directories_and_markers(self):
        first = self.install("Test.A")
        second = self.install("Test.B")
        plan = core.ModStatePlan(self.game, self.rows(), False)
        original_replace = core.os.replace
        failed = False
        def move(source, destination):
            nonlocal failed
            if Path(source) == second and not failed:
                failed = True
                raise OSError("simulated locked file")
            return original_replace(source, destination)
        with patch("core.os.replace", side_effect=move), self.assertRaises(OSError):
            plan.execute()
        self.assertTrue(first.exists() and second.exists())
        self.assertFalse(any(self.game.glob("Mods/*/.steward-disabled.json")))
        self.assertTrue(all(not m.disabled for m in self.rows()))
        self.assertEqual(core.backup_list(self.game)[0]["state"], "rolled_back")

    def test_marker_write_failure_rolls_back_without_losing_config(self):
        original = self.install("Test.A")
        real_marker = core._state_marker
        failed = False
        def marker(folder, encoded):
            nonlocal failed
            if encoded is not None and not failed:
                failed = True
                raise OSError("simulated marker access denied")
            return real_marker(folder, encoded)
        with patch("core._state_marker", side_effect=marker), self.assertRaises(OSError):
            core.ModStatePlan(self.game, self.rows(), False).execute()
        self.assertEqual((original / "config.json").read_bytes(), b'{"setting":"mine"}')
        self.assertFalse((original / ".steward-disabled.json").exists())

    def test_prepared_operation_can_recover_after_move_before_marker_write(self):
        self.install("Test.A")
        plan = core.ModStatePlan(self.game, self.rows(), False)
        folder = self.game / ".stardew-mod-steward" / "backups" / "interrupted"
        journal = dict(kind="mod_state", state="prepared", game=str(self.game), time=1,
                       enabled=False, action="禁用", entries=plan.items)
        core.write_json(folder / "transaction.json", journal)
        source, destination = core._state_paths(self.game, plan.items[0])
        destination.parent.mkdir(parents=True)
        core.os.replace(source, destination)
        core.restore_backup(self.game, folder)
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        self.assertEqual(json.loads((folder / "transaction.json").read_text())["state"], "restored")

    def test_completed_state_operation_can_be_undone_via_backups(self):
        original = self.install("Test.A")
        folder = core.ModStatePlan(self.game, self.rows(), False).execute()
        core.restore_backup(self.game, folder)
        self.assertTrue(original.exists())
        self.assertFalse(self.row("Test.A").disabled)
        self.assertEqual(json.loads((folder / "transaction.json").read_text())["state"], "restored")

    def test_old_disable_record_is_supported(self):
        original = self.install("Test.A", folder="Original folder")
        disabled = self.game / ".stardew-mod-steward" / "disabled" / "Legacy"
        disabled.parent.mkdir(parents=True)
        core.os.replace(original, disabled)
        core.write_json(disabled / ".steward-disabled.json", {"relative": "Original folder"})
        core.ModStatePlan(self.game, self.rows(), True).execute()
        self.assertTrue(original.exists())

    def test_marker_path_traversal_is_rejected(self):
        self.install("Test.A")
        core.ModStatePlan(self.game, self.rows(), False).execute()
        mod = self.row("Test.A")
        core.write_json(mod.path / ".steward-disabled.json", {"relative": "../../outside"})
        with self.assertRaises(core.ModError):
            core.ModStatePlan(self.game, [mod], True)
        self.assertTrue(mod.path.exists())

    def test_game_running_blocks_state_change(self):
        original = self.install("Test.A")
        rows = self.rows()
        with patch("core.game_running", return_value=True), self.assertRaises(core.ModError):
            core.ModStatePlan(self.game, rows, False)
        self.assertTrue(original.exists())

    def test_changed_manifest_after_confirmation_is_rejected(self):
        original = self.install("Test.A")
        plan = core.ModStatePlan(self.game, self.rows(), False)
        core.write_json(original / "manifest.json", dict(Name="Test.A", UniqueID="Test.A", Version="2.0.0"))
        with self.assertRaisesRegex(core.ModError, "列表状态已改变"):
            plan.execute()
        self.assertTrue(original.exists())

    def test_enable_never_overwrites_an_existing_target(self):
        original = self.install("Test.A")
        core.ModStatePlan(self.game, self.rows(), False).execute()
        original.mkdir()
        (original / "mine.txt").write_bytes(b"keep")
        with self.assertRaises(core.ModError):
            core.ModStatePlan(self.game, self.rows(), True)
        self.assertEqual((original / "mine.txt").read_bytes(), b"keep")

    def test_nested_mods_are_blocked_without_moves(self):
        parent = self.install("Test.Parent")
        self.install("Test.Child", folder="Test.Parent/Child")
        with self.assertRaisesRegex(core.ModError, "目录嵌套"):
            core.ModStatePlan(self.game, [self.row("Test.Parent")], False)
        self.assertTrue((parent / "Child").exists())

    def test_repeated_desired_state_does_not_create_a_transaction(self):
        self.install("Test.A")
        self.assertIsNone(core.ModStatePlan(self.game, self.rows(), True).execute())
        self.assertFalse(core.backup_list(self.game))

    def test_manually_hidden_directory_is_explained_and_not_moved(self):
        original = self.install("Test.A", folder=".ManuallyDisabled")
        with self.assertRaisesRegex(core.ModError, "外部方式隐藏"):
            core.ModStatePlan(self.game, self.rows(), True)
        self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
