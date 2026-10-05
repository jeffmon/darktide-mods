# Darktide Mods

A small window that keeps a Darktide Mod Loader install current. One file,
stdlib Python (3.11+, tkinter); no dependencies.

```
pythonw darktide_mods.py          # the window
python darktide_mods.py --help    # check / install / crash / patch / enable / disable / list
```

## What it does

| Job | How |
|---|---|
| **Check for updates** | Nexus's public GraphQL API (`api.nexusmods.com/v2/graphql`, no key) gives every file's version, date and changelog. Runs on open; *Check updates* reruns it. Select a mod to read every changelog since your version. |
| **Download** | *Not automated* - Nexus only serves downloads to logged-in users. *Get update ↗* opens the exact file page (selected mods, or every outdated one); click Download there. |
| **Install** | Nexus names zips `<Name> <mod id> <version> <date> <hash>.zip`, so the app watches Downloads and installs a newer zip for an installed mod by itself (untick *Auto-install downloads* to do it by hand). The old folder goes to `data/backups/`; user files the new zip lacks (not `.lua`/`.mod`) are carried over. Never while Darktide is running. |
| **New mods** | A Nexus zip for a mod that isn't installed is installed too and added to the load order (*New mods too*, on by default). Only zips that arrive after the app first saw Downloads count: the first run records every zip already there in `seen_downloads`, because Downloads keeps zips of mods removed on purpose. A zip modified in the last 5 s is skipped as possibly still downloading. |
| **Roll back** | Restores the newest backup of the selected mod (the current copy is backed up too). |
| **Load order** | *Enable / disable* comments the line out (`-- name  -- disabled <date>`) instead of deleting it, so its position survives. New mods load before anything in `load_last` in `data/state.json` (`vfx_swapper`, whose effect swaps must load last). |
| **Game updates** | Steam's update undoes the DML patch. The banner notices the build change; *Patch && Launch* re-runs `tools/dtkit-patch.exe --patch` (safe when already patched) and starts the game. |
| **Crash report** | Reads the newest console log, names the first mod in the crashing Lua stack, and lists errors mods logged through DMF. |

## Versions

A mod's installed version comes from, in order: what this app recorded when
it installed it, `info.json`, then the `.mod` file. `.mod` versions are often
stale, which is why `info.json` beats them. A recorded version is tied to the
mod file's timestamp, so updating a mod by hand makes the app fall back to the
files instead of reporting the old version. Mods with no version anywhere show
`?` - use *Set version…* once.

`data/` (state, backups) is machine-specific and gitignored.

Tests: `python -m unittest test_darktide_mods -v`

## Packaging for someone else

```
python -m venv .build-venv
.build-venv/Scripts/python -m pip install pyinstaller
.build-venv/Scripts/python -m PyInstaller --onedir --windowed --name DarktideMods --noconfirm --clean darktide_mods.py
```

PyInstaller is a build tool only, kept in the gitignored venv; the app
itself stays stdlib. Ship the `dist/DarktideMods/` folder with `READ ME.txt`.
The exe keeps its state in `%LOCALAPPDATA%\DarktideMods`; run from source,
state stays in `data/`.

Folder mode (`--onedir`), not `--onefile`: Defender quarantined the onefile
build twice on 2026-09-29/10-04 (`Trojan:Win32/Bearfoos.A!ml`,
`Behavior:Win32/DefenseEvasion.A!ml`), both machine-learning verdicts raised
at launch. A onefile exe unpacks itself to %TEMP% and relaunches as a child
process, which is what dropper malware does; folder mode does neither. It
is still unsigned, so SmartScreen warns on first launch, and a heuristic
can still misfire - READ ME.txt tells the user how to allow it.

## Releasing

Every update ships as a GitHub release with the zip attached:

1. Bump `APP_VERSION` in `darktide_mods.py` and commit.
2. `python release.py` - runs the tests, builds, zips
   `DarktideMods-v<version>.zip`, pushes, tags `v<version>`, creates the
   release with the commit subjects since the last tag as notes, and
   refreshes the Desktop copies. `--dry-run` builds without publishing.

Latest download for anyone:
https://github.com/jeffmon/darktide-mods/releases/latest
