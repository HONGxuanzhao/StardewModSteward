"""Safety regression tests. All filesystem mutations use temporary fake games."""
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch
import zipfile

import core


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.game = self.root / "game"
        self.mods = self.game / "Mods"
        self.mods.mkdir(parents=True)
        (self.game / "StardewModdingAPI.exe").write_bytes(b"test")
        self.patches = [patch("core.installed_versions", return_value=("4.5.2", "1.6.15")),
                        patch("core.game_running", return_value=False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def manifest(self, uid="Example.Mod", version="1.0.0", **extra):
        return dict(Name=uid, UniqueID=uid, Version=version, Author="Test", **extra)

    def installed(self, uid="Example.Mod", version="1.0.0", folder="Example", **extra):
        root = self.mods / folder
        root.mkdir(parents=True)
        core.write_json(root / "manifest.json", self.manifest(uid, version, **extra))
        (root / "old.dll").write_bytes(b"old binary")
        return root

    def archive(self, mods=None, other=None, name="update.zip"):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as z:
            for folder, data in (mods or {"Example": self.manifest(version="2.0.0")}).items():
                z.writestr(folder + "/manifest.json", json.dumps(data))
                z.writestr(folder + "/new.dll", b"new binary")
                z.writestr(folder + "/config.json", '{"setting": "default"}')
            for key, value in (other or {}).items():
                z.writestr(key, value)
        return path

    def test_jsonc_strings_comments_trailing_commas_and_case(self):
        p = self.root / "manifest.json"
        p.write_text('''{ // comment
          "name":"A", "uniqueId":"Example.A", "version":"1.0", /* note */
          "Description":"https://example.com/a//b, } /* string */", "UpdateKeys":["Nexus:1",],
        }''', encoding="utf-8-sig")
        mod = core.mod_from_manifest(p)
        self.assertEqual(mod.uid, "Example.A")
        self.assertIn("/* string */", mod.description)
        self.assertEqual(mod.keys, ["Nexus:1"])

    def test_scan_duplicates_required_dependency(self):
        self.installed("Duplicate.ID", folder="One")
        self.installed("duplicate.id", folder="Two")
        self.installed("Needs.Other", folder="Three", Dependencies=[{"UniqueID": "Missing.Mod"}])
        rows, errors = core.scan(self.game)
        self.assertEqual(len(rows), 3)
        self.assertFalse(errors)
        self.assertTrue(all(m.issues for m in rows))

    def test_update_replaces_old_code_preserves_config_and_restores(self):
        old = self.installed()
        (old / "config.json").write_text('{"setting":"mine"}')
        archive = self.archive()
        plan = core.InstallPlan(self.game, archive, {"Example.Mod": "2.0.0"})
        backup = plan.execute()
        self.assertFalse((old / "old.dll").exists())
        self.assertTrue((old / "new.dll").exists())
        self.assertEqual(json.loads((old / "config.json").read_text())["setting"], "mine")
        self.assertTrue((backup / "old-0" / "old.dll").exists())
        previous = core.restore_backup(self.game, backup)
        self.assertTrue((old / "old.dll").exists())
        self.assertFalse((old / "new.dll").exists())
        self.assertTrue(any(previous.rglob("new.dll")))

    def test_new_install_has_recoverable_undo(self):
        plan = core.InstallPlan(self.game, self.archive())
        target = plan.items[0].target
        backup = plan.execute()
        self.assertTrue(target.exists())
        previous = core.restore_backup(self.game, backup)
        self.assertFalse(target.exists())
        self.assertTrue(any(previous.rglob("manifest.json")))

    def test_failure_rolls_back_original(self):
        old = self.installed()
        plan = core.InstallPlan(self.game, self.archive())
        original_replace = core.os.replace
        def fail_once(src, dst):
            if Path(src).name.startswith("ready-"):
                raise OSError("simulated access denied")
            return original_replace(src, dst)
        with patch("core.os.replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                plan.execute()
        self.assertTrue((old / "old.dll").exists())
        self.assertEqual(core.mod_from_manifest(old / "manifest.json").version, "1.0.0")
        self.assertEqual(core.backup_list(self.game)[0]["state"], "rolled_back")

    def test_reject_traversal_before_mod_changes(self):
        old = self.installed()
        package = self.archive(other={"../outside.txt": "bad"})
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, package)
        self.assertTrue((old / "old.dll").exists())
        self.assertFalse((self.game / "outside.txt").exists())

    def test_windows_archive_names_rejected(self):
        for name in ["a/../../evil", "C:/evil", "a/CON.txt", "a/evil:stream", "a/name."]:
            with self.subTest(name=name), self.assertRaises(core.ModError):
                core.validate_member(name)

    def test_duplicate_existing_blocks_install(self):
        self.installed(folder="One")
        self.installed(folder="Two")
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, self.archive())

    def test_nested_source_and_installed_directories_blocked(self):
        self.installed()
        self.installed("Other.Mod", folder="Example/Nested")
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, self.archive())
        package = self.archive(mods={"Parent": self.manifest("Parent.Mod"),
                                    "Parent/Nested": self.manifest("Nested.Mod")}, name="nested.zip")
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, package)

    def test_mismatch_downgrade_missing_dependency_blocked(self):
        self.installed(version="2.0.0")
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, self.archive(), {"Example.Mod": "3.0.0"})
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, self.archive({"Example": self.manifest(version="1.0.0")}))
        dep = self.archive({"Dependent": self.manifest("Dep.Mod", Dependencies=[{"UniqueID": "Missing.Mod"}])})
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, dep)

    def test_bundled_required_dependency_install(self):
        package = self.archive({"One": self.manifest("One.Mod", Dependencies=[{"UniqueID": "Two.Mod"}]),
                                "Two": self.manifest("Two.Mod")})
        plan = core.InstallPlan(self.game, package)
        self.assertEqual(len(plan.items), 2)
        plan.execute()
        self.assertEqual(len(core.scan(self.game)[0]), 2)

    def test_game_running_blocks_mutation(self):
        self.installed()
        plan = core.InstallPlan(self.game, self.archive())
        with patch("core.game_running", return_value=True), self.assertRaises(core.ModError):
            plan.execute()
        plan.close()

    def test_disable_and_enable_restores_original_path(self):
        original = self.installed(folder="Folder with spaces")
        mod = core.scan(self.game)[0][0]
        disabled = core.toggle_mod(self.game, mod)
        self.assertFalse(original.exists())
        self.assertTrue(disabled.exists())
        mod = core.scan(self.game)[0][0]
        self.assertTrue(mod.disabled)
        restored = core.toggle_mod(self.game, mod)
        self.assertEqual(restored, original)
        self.assertTrue((original / "old.dll").exists())

    def test_update_request_and_unknown_results_not_marked_current(self):
        self.installed()
        mods = core.scan(self.game)[0]
        with patch("core.request_json", return_value=[]) as req:
            cache = core.check_updates(self.game, mods)
        self.assertEqual(req.call_args.kwargs["json"]["mods"][0]["installedVersion"], "1.0.0")
        core.apply_cache(mods, cache, self.game)
        self.assertEqual(mods[0].status, "检查失败")
        mods[0].version = "2.0.0"
        mods[0].update = {}
        core.apply_cache(mods, cache, self.game)
        self.assertFalse(mods[0].update)

    def test_nexus_main_exact_version_and_subkey_selection(self):
        self.installed(UpdateKeys=["Nexus:123@Correct"])
        mod = core.scan(self.game)[0][0]
        mod.update = {"suggestedUpdate": {"version": "2.0.0", "url": "https://www.nexusmods.com/stardewvalley/mods/123"}}
        files = [dict(file_id=1, category_id=1, version="2.0.0", name="Main @Correct"),
                 dict(file_id=2, category_id=1, version="2.0.0", name="Main @Wrong"),
                 dict(file_id=3, category_id=3, version="2.0.0", name="Old @Correct"),
                 dict(file_id=4, category_id=1, version="3.0.0", name="Main @Correct")]
        with patch.object(core.Nexus, "files", return_value=files):
            self.assertEqual(core.Nexus("dummy").candidates(mod)[0]["file_id"], 1)

    def test_dpapi_round_trip(self):
        if core.os.name != "nt":
            self.skipTest("Windows DPAPI")
        value = "test-not-a-real-key"
        protected = core.protect_key(value)
        self.assertNotIn(value, protected)
        self.assertEqual(core.protect_key(protected, decrypt=True), value)

    def test_incomplete_transaction_blocks_new_install(self):
        self.installed()
        folder = self.game / ".stardew-mod-steward" / "backups" / "interrupted"
        core.write_json(folder / "transaction.json", {"kind": "install", "state": "prepared", "time": 1,
                                                       "game": str(self.game), "entries": []})
        with self.assertRaises(core.ModError):
            core.InstallPlan(self.game, self.archive())

    def test_local_7zip_extract(self):
        exe = Path(r"C:\Program Files\7-Zip\7z.exe")
        if not exe.exists():
            self.skipTest("7-Zip not installed")
        source = self.root / "input"
        source.mkdir()
        (source / "hello.txt").write_text("payload")
        archive = self.root / "test.7z"
        result = subprocess.run([str(exe), "a", str(archive), "hello.txt"], cwd=source, capture_output=True,
                                creationflags=core.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0)
        dest = self.root / "unpacked"
        core.extract_archive(archive, dest)
        self.assertEqual((dest / "hello.txt").read_text(), "payload")

    def test_update_key_validation_ignores_placeholders_and_keeps_subkeys(self):
        valid, ignored = core.validated_update_keys(["Nexus:???", "Nexus:-1", "-1", "Nexus:Ignore This",
            "ferngill.simple.economy", " nexus: 19508 ", "Nexus:23135@Main", "GitHub:Owner/Repo@Pack", "Nexus:0"])
        self.assertEqual(valid, ["Nexus:19508", "Nexus:23135@Main", "GitHub:Owner/Repo@Pack"])
        self.assertEqual(len(ignored), 6)

    def test_bad_source_keys_not_sent_and_manifest_not_modified(self):
        root = self.installed(UpdateKeys=["Nexus:???", "Nexus: 123"])
        original = (root / "manifest.json").read_bytes()
        mods = core.scan(self.game)[0]
        with patch("core.request_json", return_value=[{"id": "Example.Mod", "errors": [],
                  "metadata": {"main": {"version": "1.0.0", "url": "https://www.nexusmods.com/stardewvalley/mods/123"}}}]) as req:
            cache = core.check_updates(self.game, mods)
        self.assertEqual(req.call_args.kwargs["json"]["mods"][0]["updateKeys"], ["Nexus:123"])
        core.apply_cache(mods, cache, self.game)
        self.assertEqual(mods[0].status, "无推荐更新")
        self.assertEqual(mods[0].update["ignoredUpdateKeys"], ["Nexus:???"])
        self.assertEqual((root / "manifest.json").read_bytes(), original)

    def test_partial_source_error_preserved_without_false_failure(self):
        self.installed()
        mod = core.scan(self.game)[0][0]
        mod.update = core.prepare_update_result(mod, {"id": mod.uid, "errors": ["Found no Nexus mod with this ID."],
            "metadata": {"main": {"version": "1.0.0", "url": "https://www.moddrop.com/stardew-valley/mods/1"},
                         "compatibilityStatus": "Abandoned"}})
        self.assertFalse(mod.update["errors"])
        self.assertEqual(len(mod.update["sourceWarnings"]), 1)
        self.assertEqual(mod.status, "停止维护")
        self.assertEqual(mod.source_label, "ModDrop")
        self.assertTrue(mod.requires_attention)

    def test_real_source_failure_without_version_remains_failed(self):
        self.installed()
        mod = core.scan(self.game)[0][0]
        mod.update = core.prepare_update_result(mod, {"id": mod.uid, "errors": ["not found"], "metadata": {}})
        self.assertEqual(mod.status, "检查失败")
        self.assertTrue(mod.requires_attention)

    def test_installed_known_unofficial_fix_is_not_marked_broken(self):
        self.installed(version="1.1.3-unofficial.1-author")
        mod = core.scan(self.game)[0][0]
        mod.update = {"metadata": {"compatibilityStatus": "Unofficial", "unofficial": {"version": "1.1.3-unofficial.1-author"}}}
        self.assertEqual(mod.status, "已装修复版")
        self.assertFalse(mod.requires_attention)
        mod.version = "1.1.2"
        self.assertEqual(mod.status, "兼容性提示")
        self.assertTrue(mod.requires_attention)

    def test_bundle_versions_not_substituted_for_component_versions(self):
        self.installed("atravita.EastScarp", "3.0.9", folder="Package/Main", UpdateKeys=["-1"])
        self.installed("LemurKat.EastScarpe.CP", "3.0.5", folder="Package/Fix", UpdateKeys=["Nexus:???"])
        mods = core.scan(self.game)[0]
        result = {m.uid.casefold(): core.prepare_update_result(m, {"id": m.uid, "errors": [], "metadata": {}}) for m in mods}
        result["atravita.eastscarp"]["metadata"] = {"id": ["atravita.EastScarp"],
            "main": {"version": "3.0.9", "url": "https://www.nexusmods.com/stardewvalley/mods/5787"}}
        core.associate_bundles(mods, result)
        component = next(m for m in mods if m.uid.casefold() != "atravita.eastscarp")
        component.update = result[component.uid.casefold()]
        self.assertEqual(component.version, "3.0.5")
        self.assertFalse(component.suggestion)
        self.assertEqual(component.status, "需网页核对")
        self.assertEqual(component.update["bundle"]["remoteVersion"], "3.0.9")

    def test_bundle_association_requires_verified_primary_and_same_parent(self):
        self.installed("FlashShifter.SVECode", folder="One/Main")
        self.installed("FlashShifter.SVE-FTM", folder="Other/Component", UpdateKeys=["Nexus:???"])
        mods = core.scan(self.game)[0]
        result = {m.uid.casefold(): core.prepare_update_result(m, {"id": m.uid, "metadata": {}}) for m in mods}
        result["flashshifter.svecode"]["metadata"] = {"id": ["FlashShifter.SVECode"],
            "main": {"version": "1.0.0", "url": "https://www.nexusmods.com/stardewvalley/mods/3753"}}
        core.associate_bundles(mods, result)
        self.assertNotIn("bundle", result["flashshifter.sve-ftm"])

    def test_cache_schema_and_source_keys_are_checked(self):
        self.installed(UpdateKeys=["Nexus:1"])
        mods = core.scan(self.game)[0]
        with patch("core.request_json", return_value=[{"id": "Example.Mod", "metadata": {}, "errors": []}]):
            cache = core.check_updates(self.game, mods)
        old = dict(cache, schema=1)
        core.apply_cache(mods, old, self.game)
        self.assertFalse(mods[0].update)
        mods[0].manifest["UpdateKeys"] = ["Nexus:2"]
        core.apply_cache(mods, cache, self.game)
        self.assertFalse(mods[0].update)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_game(self, library, name="Stardew Valley", played=1):
        game = library / "steamapps" / "common" / name
        (game / "Mods").mkdir(parents=True)
        (game / "StardewModdingAPI.exe").write_bytes(b"fixture")
        manifest = f'"AppState" {{ "appid" "413150" "installdir" "{name}" "LastPlayed" "{played}" }}'
        (library / "steamapps" / "appmanifest_413150.acf").write_text(manifest)
        return game

    def test_steam_modern_library_discovery_without_fixed_drive(self):
        steam = self.root / "Steam client"
        library = self.root / "Other disk library"
        game = self.make_game(library, "Custom Stardew Directory", 20)
        (steam / "steamapps").mkdir(parents=True)
        escaped = str(library).replace("\\", "\\\\")
        (steam / "steamapps" / "libraryfolders.vdf").write_text(f'"libraryfolders" {{ "1" {{ "path" "{escaped}" }} }}')
        found = core.discover_games(steam_roots=[steam], include_system=False)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["game"], str(game))
        self.assertEqual(found[0]["mods"], str(game / "Mods"))

    def test_multiple_installations_rank_last_played_then_explicit_selection(self):
        first, second = self.root / "A", self.root / "B"
        game_a = self.make_game(first, played=10)
        game_b = self.make_game(second, played=20)
        found = core.discover_games(steam_roots=[first, second], include_system=False)
        self.assertEqual(found[0]["game"], str(game_b))
        selected = core.discover_games([game_a / "Mods"], steam_roots=[first, second], include_system=False)
        self.assertEqual(selected[0]["game"], str(game_a))
        self.assertEqual(core.validate_game(game_a / "Mods"), game_a)

    def test_legacy_vdf_and_incomplete_game_not_accepted(self):
        steam = self.root / "Steam"
        library = self.root / "Library"
        game = self.make_game(library)
        (game / "StardewModdingAPI.exe").unlink()
        (steam / "steamapps").mkdir(parents=True)
        escaped = str(library).replace("\\", "\\\\")
        (steam / "steamapps" / "libraryfolders.vdf").write_text(f'"LibraryFolders" {{ "1" "{escaped}" }}')
        self.assertEqual(core.discover_games(steam_roots=[steam], include_system=False), [])

    def test_settings_recovers_missing_saved_path_preserves_other_preferences(self):
        game = self.make_game(self.root / "Library")
        data = self.root / "settings"
        core.write_json(data / "settings.json", {"game": str(self.root / "Missing"), "auto_check": False,
                                                 "preserve_config": False, "nexus_key": "encrypted-sentinel"})
        found = [{"game": str(game), "mods": str(game / "Mods"), "sources": ["Steam 库"]}]
        with patch("core.discover_games", return_value=found):
            settings = core.Settings(data)
        self.assertEqual(settings.values["game"], str(game))
        self.assertEqual(settings.values["nexus_key"], "encrypted-sentinel")
        self.assertFalse(settings.values["preserve_config"])
        self.assertFalse(settings.values["auto_check"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
