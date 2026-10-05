"""Tests for the parts of darktide_mods that do not touch Nexus or the GUI.

Run: python -m unittest test_darktide_mods -v
"""

import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import darktide_mods as dm


class VersionTests(unittest.TestCase):
    def test_numeric_parts_compare_as_numbers(self):
        self.assertTrue(dm.is_newer("1.10", "1.9"))
        self.assertTrue(dm.is_newer("26.09.29.1", "26.09.29"))
        self.assertTrue(dm.is_newer("6.5.0b", "6.0.0b"))
        self.assertTrue(dm.is_newer("1.3", "1.2.7"))

    def test_leading_zero_is_the_same_version(self):
        self.assertFalse(dm.is_newer("1.02", "1.2"))
        self.assertFalse(dm.is_newer("1.2", "1.02"))

    def test_unknown_is_never_newer(self):
        self.assertFalse(dm.is_newer("1.0", None))
        self.assertFalse(dm.is_newer(None, "1.0"))


class DownloadNameTests(unittest.TestCase):
    def test_current_nexus_name(self):
        self.assertEqual(
            dm.parse_download("Vfx Swapper 678 1.3 2026-09-29T20-52Z RZfXOZuui.zip"),
            (678, "1.3", "2026-09-29T20-52Z"))

    def test_name_containing_a_version(self):
        self.assertEqual(
            dm.parse_download(
                "Vfx Swapper 1.2.7 678 1.2.7 2026-08-20T15-36Z NBtE5BTsd.zip")[:2],
            (678, "1.2.7"))

    def test_browser_duplicate_suffix(self):
        got = dm.parse_download(
            "Enhanced Descriptions 210 6.0.0b 2026-07-17T18-02Z YFIfRFOFf(1).zip")
        self.assertEqual(got[:2], (210, "6.0.0b"))

    def test_legacy_api_name(self):
        self.assertEqual(
            dm.parse_download("Enemies Improved-809-1-3-01-1776381380.zip")[:2],
            (809, "1.3.01"))

    def test_unrelated_zip(self):
        self.assertIsNone(dm.parse_download("saints-of-the-slaughter-windows.zip"))
        self.assertIsNone(dm.parse_download("butler-windows-amd64.zip"))

    def test_newest_zip_wins(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ["Alfs DMF Extensions 864 1.2.02 2026-06-23T14-11Z a.zip",
                      "Alfs DMF Extensions 864 2.0.7 2026-09-19T21-09Z b.zip"]:
                (Path(d) / n).write_bytes(b"")
                os.utime(Path(d) / n, (1, 1))   # past SETTLE_SECONDS
            self.assertEqual(dm.scan_downloads(Path(d))[864].version, "2.0.7")


LOAD_ORDER = """-- ################################################################
-- Enter user mod names below, separated by line.
-- ################################################################
scoreboard
-- scores  -- disabled 2026-08-22: reverted to scoreboard (do not run both)
NumericUI
vfx_swapper""".splitlines()
FOLDERS = {"scoreboard", "scores", "NumericUI", "vfx_swapper", "Healthbars", "Enter"}


class LoadOrderTests(unittest.TestCase):
    def test_status_ignores_header_comments(self):
        st = dm.load_order_status(LOAD_ORDER, FOLDERS)
        self.assertEqual(st, {"scoreboard": True, "scores": False,
                              "NumericUI": True, "vfx_swapper": True})

    def test_disable_comments_the_line_in_place(self):
        out = dm.set_enabled(LOAD_ORDER, "NumericUI", False, FOLDERS, [],
                             today="2026-09-29")
        self.assertEqual(out[5], "-- NumericUI  -- disabled 2026-09-29")
        self.assertEqual(len(out), len(LOAD_ORDER))

    def test_enable_uncomments_in_place(self):
        out = dm.set_enabled(LOAD_ORDER, "scores", True, FOLDERS, [])
        self.assertEqual(out[4], "scores")

    def test_new_mod_goes_before_load_last(self):
        out = dm.set_enabled(LOAD_ORDER, "Healthbars", True, FOLDERS,
                             ["vfx_swapper"])
        self.assertEqual(out[-2:], ["Healthbars", "vfx_swapper"])

    def test_new_mod_appends_without_load_last(self):
        out = dm.set_enabled(LOAD_ORDER, "Healthbars", True, FOLDERS, [])
        self.assertEqual(out[-1], "Healthbars")

    def test_disabling_an_absent_mod_changes_nothing(self):
        self.assertEqual(
            dm.set_enabled(LOAD_ORDER, "Healthbars", False, FOLDERS, []),
            LOAD_ORDER)


# Shaped like the real 2026-09-30 crash: the message appears first on its
# own, the stack only follows the repeat as a Script Error.
CRASH_LOG = """
05:54:38.863 [Lua] <<crashify-property>>game_version = 1.13.0-b802981<</crashify-property>>
05:54:38.863 [Lua] <<crashify-property>>is_modded = true<</crashify-property>>
05:54:40.296 [Lua] [MOD][see_through_ogryns][ERROR] (localize) "amount (%)": invalid option
06:02:09.297 <<Lua Error>>...player_husk_data_extension.lua:277: attempt to index local 'field' (a nil value)<</Lua Error>>
lots of other lines
<<Script Error>>...player_husk_data_extension.lua:277: attempt to index local 'field' (a nil value)<</Script Error>>
<<Lua Stack>>  [1] @scripts/extension_systems/unit_data/player_husk_data_extension.lua:277: in function __index
  [2] ./../mods/NumericUI/scripts/mods/NumericUI/TeamPlayerPanel.lua:507: in function hook_chain
  [3] ./../mods/dmf/scripts/mods/dmf/modules/core/hooks.lua:199: in function init
  [6] ./../mods/scoreboard/scripts/x.lua:1: in function y
<</Lua Stack>>
"""


class CrashLogTests(unittest.TestCase):
    def test_names_the_mod_from_the_later_stack(self):
        rep = dm.analyse_log(CRASH_LOG)
        self.assertEqual(len(rep.errors), 1)
        self.assertEqual(rep.errors[0].suspect, "NumericUI")
        self.assertEqual(rep.errors[0].mods_in_stack, ["NumericUI", "scoreboard"])

    def test_header_fields_and_dmf_errors(self):
        rep = dm.analyse_log(CRASH_LOG)
        self.assertEqual(rep.game_version, "1.13.0-b802981")
        self.assertTrue(rep.modded)
        self.assertIn("see_through_ogryns", rep.mod_errors)

    def test_clean_log(self):
        rep = dm.analyse_log("nothing to see")
        self.assertEqual(rep.errors, [])
        self.assertIsNone(rep.modded)


class RemoteTests(unittest.TestCase):
    def mod(self):
        f = dm.NexusFile
        return dm.RemoteMod(678, [
            f(1, "Vfx", "1.2.7", 100, "OLD_VERSION"),
            f(2, "Vfx", "1.2.8", 200, "OLD_VERSION", ["crash fix"]),
            f(3, "Vfx", "1.3", 300, "MAIN", ["Rotten Armor fix"]),
            f(4, "Vfx addon", "9.9", 400, "OPTIONAL"),
        ])

    def test_latest_is_newest_main_file(self):
        self.assertEqual(self.mod().latest.version, "1.3")

    def test_newer_than_skips_optional_and_older(self):
        self.assertEqual([f.version for f in self.mod().newer_than("1.2.7")],
                         ["1.3", "1.2.8"])


CATALOG = [
    {"modId": 22, "name": "Scoreboard"},
    {"modId": 65, "name": "Scoreboard (ko-kr)"},
    {"modId": 127, "name": "Who Are You - Display account names"},
    {"modId": 809, "name": "Enemies Improved (Healthbars - Debuffs - Outlines and more)"},
    {"modId": 864, "name": "Alf's DMF (Mod Settings) Extensions"},
    {"modId": 48, "name": "(OBSOLETE) Crosshair Remap"},
    {"modId": 253, "name": "Crosshair Remap (Continued)"},
    {"modId": 500, "name": "Better Timer"},
    {"modId": 501, "name": "Better Timer"},
]


class NameMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.index = dm.build_catalog_index(CATALOG)

    def match(self, folder, loc_name=None):
        f = Path(self.tmp.name) / folder
        f.mkdir()
        if loc_name:
            loc = f / "scripts" / "mods" / folder
            loc.mkdir(parents=True)
            (loc / f"{folder}_localization.lua").write_text(
                f'local mod_name = {{\n    en = "{loc_name}",\n}}')
        hit = dm.match_by_name(f, self.index)
        return hit[0] if hit else None

    def test_sales_copy_after_dash_or_in_brackets(self):
        self.assertEqual(self.match("who_are_you"), 127)
        self.assertEqual(self.match("enemies_improved"), 809)
        self.assertEqual(self.match("Alfs_DMF_Extensions"), 864)

    def test_exact_title_beats_translation(self):
        self.assertEqual(self.match("scoreboard"), 22)

    def test_obsolete_original_loses_to_fork(self):
        self.assertEqual(self.match("crosshair_remap"), 253)

    def test_true_ambiguity_is_left_alone(self):
        self.assertIsNone(self.match("better_timer"))

    def test_localization_name_is_used(self):
        self.assertEqual(self.match("wau", loc_name="Who Are You"), 127)

    def test_link_zero_means_custom(self):
        f = Path(self.tmp.name) / "scoreboard"
        f.mkdir()
        self.assertEqual(
            dm.resolve_nexus_id(f, None, {"nexus_ids": {"scoreboard": 0}}, self.index),
            (None, "custom"))


def make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)
    return path


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        patches = [mock.patch.object(dm, "DATA_DIR", self.data),
                   mock.patch.object(dm, "BACKUP_DIR", self.data / "backups"),
                   mock.patch.object(dm, "STATE_PATH", self.data / "state.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        game = base / "game"
        (game / "mods" / "vfx_swapper").mkdir(parents=True)
        (game / "mods" / "vfx_swapper" / "vfx_swapper.mod").write_text('version = "1.2.6"')
        (game / "mods" / "vfx_swapper" / "old.lua").write_text("old")
        (game / "mods" / "vfx_swapper" / "preset.cfg").write_text("mine")
        (game / "mods" / "mod_load_order.txt").write_text("vfx_swapper\n")
        self.paths = dm.Paths(game=game, downloads=base / "dl", logs=base / "logs")
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def test_update_backs_up_and_records_version(self):
        z = make_zip(self.base / "v.zip", {
            "vfx_swapper/vfx_swapper.mod": 'version = "1.3"',
            "vfx_swapper/info.json": json.dumps({"version": "1.3"}),
            "vfx_swapper/scripts/new.lua": "new"})
        state = {}
        dm.install_zip(self.paths, state, z, "1.3", 678, log=lambda m: None,
                       check_running=False)
        dest = self.paths.mods / "vfx_swapper"
        self.assertTrue((dest / "scripts" / "new.lua").exists())
        self.assertFalse((dest / "old.lua").exists(), "stale code is dropped")
        self.assertEqual((dest / "preset.cfg").read_text(), "mine",
                         "user files are carried over")
        self.assertEqual(len(dm.list_backups("vfx_swapper")), 1)
        mods = {m.folder: m for m in dm.scan_mods(self.paths, state)}
        self.assertEqual(mods["vfx_swapper"].version, "1.3")

    def test_new_mod_is_added_to_load_order(self):
        z = make_zip(self.base / "h.zip", {"Healthbars/Healthbars.mod": ""})
        dm.install_zip(self.paths, {}, z, "26.09.29", 16, log=lambda m: None,
                       check_running=False)
        self.assertEqual(self.paths.load_order.read_text().split(),
                         ["Healthbars", "vfx_swapper"],
                         "new mods load above vfx_swapper by default")

    def test_update_leaves_an_unlisted_mod_off(self):
        # Healthbars on jeffmon's PC: installed, deliberately not in the list.
        (self.paths.mods / "Healthbars").mkdir()
        (self.paths.mods / "Healthbars" / "Healthbars.mod").write_text("")
        z = make_zip(self.base / "h.zip", {"Healthbars/Healthbars.mod": "new"})
        dm.install_zip(self.paths, {}, z, "26.09.29", 16, log=lambda m: None,
                       check_running=False)
        self.assertEqual(self.paths.load_order.read_text().split(),
                         ["vfx_swapper"])

    def test_rollback_restores_previous_copy(self):
        z = make_zip(self.base / "v.zip", {"vfx_swapper/vfx_swapper.mod": 'version = "1.3"'})
        dm.install_zip(self.paths, {}, z, "1.3", 678, log=lambda m: None,
                       check_running=False)
        with mock.patch.object(dm, "game_running", return_value=False):
            dm.rollback(self.paths, {}, "vfx_swapper", log=lambda m: None)
        self.assertTrue((self.paths.mods / "vfx_swapper" / "old.lua").exists())

    def _old_zip(self, name, files):
        self.paths.downloads.mkdir(exist_ok=True)
        z = make_zip(self.paths.downloads / name, files)
        import os
        os.utime(z, (1, 1))   # older than SETTLE_SECONDS
        return z

    def test_new_mod_zip_after_baseline_is_offered(self):
        state = {}
        self._old_zip("Improve Yourself 999 1.1.0 2026-08-18T10-00Z a.zip",
                      {"improve-yourself/improve-yourself.mod": ""})
        dm.baseline_downloads(state, self.paths.downloads)
        self._old_zip("LetThereBeLight 1099 1.2.0 2026-08-17T16-42Z b.zip",
                      {"LetThereBeLight/LetThereBeLight.mod": ""})
        dm.index_download_zips(self.paths.downloads, state)
        mods = dm.scan_mods(self.paths, state)
        new = dm.new_mod_downloads(mods, dm.scan_downloads(self.paths.downloads), state)
        self.assertEqual([d.nexus_id for d in new], [1099],
                         "zips there before the baseline are left alone")

    def test_installed_new_mod_is_not_offered_again(self):
        state = {"seen_downloads": []}
        z = self._old_zip("LetThereBeLight 1099 1.2.0 2026-08-17T16-42Z b.zip",
                          {"LetThereBeLight/LetThereBeLight.mod": ""})
        dm.install_zip(self.paths, state, z, "1.2.0", 1099, log=lambda m: None,
                       check_running=False)
        dm.index_download_zips(self.paths.downloads, state)
        mods = dm.scan_mods(self.paths, state)
        self.assertEqual(
            dm.new_mod_downloads(mods, dm.scan_downloads(self.paths.downloads), state), [])
        self.assertIn("LetThereBeLight", self.paths.load_order.read_text())

    def test_zip_for_existing_unmatched_folder_is_not_new(self):
        state = {"seen_downloads": []}
        self._old_zip("Vfx 4242 9.0 2026-09-29T20-52Z c.zip",
                      {"vfx_swapper/vfx_swapper.mod": ""})
        dm.index_download_zips(self.paths.downloads, state)
        mods = dm.scan_mods(self.paths, {"nexus_ids": {"vfx_swapper": 0}})
        self.assertEqual(
            dm.new_mod_downloads(mods, dm.scan_downloads(self.paths.downloads), state), [])

    def test_zip_still_being_written_is_ignored(self):
        self.paths.downloads.mkdir(exist_ok=True)
        make_zip(self.paths.downloads / "New 5 1.0 2026-10-04T10-00Z d.zip",
                 {"new/new.mod": ""})
        self.assertEqual(dm.scan_downloads(self.paths.downloads), {})

    def test_zip_slip_is_refused(self):
        z = make_zip(self.base / "bad.zip", {"../../evil.txt": "x"})
        with self.assertRaises(dm.InstallError):
            dm.install_zip(self.paths, {}, z, "1", None, log=lambda m: None,
                           check_running=False)

    def test_renamed_folder_is_refused(self):
        z = make_zip(self.base / "r.zip", {"VfxSwapper/VfxSwapper.mod": ""})
        with self.assertRaises(dm.InstallError):
            dm.install_zip(self.paths, {"nexus_ids": {"vfx_swapper": 678}}, z,
                           "1.4", 678, log=lambda m: None, check_running=False)

    def test_hand_update_invalidates_recorded_version(self):
        state = {}
        folder = self.paths.mods / "vfx_swapper"
        dm.record_version(state, "vfx_swapper", "9.9", dm.folder_stamp(folder))
        mod = folder / "vfx_swapper.mod"
        mod.write_text('version = "1.2.6"')
        import os
        os.utime(mod, ns=(1, 1))
        m = {m.folder: m for m in dm.scan_mods(self.paths, state)}["vfx_swapper"]
        self.assertEqual((m.version, m.version_source), ("1.2.6", ".mod"))


if __name__ == "__main__":
    unittest.main()
