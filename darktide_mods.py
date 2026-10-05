"""Darktide Mods - keeps a Darktide Mod Loader install current.

What it automates, and what it deliberately leaves to a human:

* Checking for updates. Nexus's public GraphQL API (no key, no login)
  returns every file of a mod with its version, date and changelog, so the
  check is exact rather than a scrape of the website (which answers 403).
* Downloading is the one step it cannot do. Nexus only hands out download
  links to logged-in users, so the app opens the right file page and you
  click Download there.
* Installing. Nexus names every zip "<Name> <mod id> <version> <date>
  <hash>.zip", so a zip in Downloads identifies itself. The app watches the
  folder and swaps the new version in, backing up the old folder first.
* The load order, the DML re-patch Steam undoes on every game update, and
  reading the console log after a crash to name the mod in the stack.

Stdlib only (CPython 3.11+, tkinter). Run with `pythonw darktide_mods.py`
for the window, or `python darktide_mods.py <command>` - see --help.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
# A PyInstaller exe unpacks itself to a temp folder that is deleted on exit,
# so the packaged build keeps its state in the user's profile instead. Run
# from source, it stays beside the script.
if getattr(sys, "frozen", False):
    DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "DarktideMods"
else:
    DATA_DIR = APP_DIR / "data"
STATE_PATH = DATA_DIR / "state.json"
BACKUP_DIR = DATA_DIR / "backups"

DARKTIDE_APP_ID = "1361210"
NEXUS_GAME_ID = 4943
NEXUS_DOMAIN = "warhammer40kdarktide"
GRAPHQL_URL = "https://api.nexusmods.com/v2/graphql"
USER_AGENT = "darktide-mods/1.0 (personal mod updater)"

DEFAULT_GAME_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\Warhammer 40,000 DARKTIDE")

# The loader owns these two folders. They never appear in the load order,
# and "base" arrives inside a game-root zip rather than a mod-folder zip.
FRAMEWORK = {"base", "dmf"}

# Nexus ids for mods whose files do not say where they came from. Newer
# mods carry an info.json with a homepage URL, which wins over this table.
KNOWN_NEXUS_IDS = {
    "base": 19, "dmf": 8, "Alfs_DMF_Extensions": 864, "animation_events": 21,
    "scoreboard": 22, "Enhanced_descriptions": 210, "psych_ward": 89,
    "enemies_improved": 809, "danger_zone": 440, "emperors_lantern": 626,
    "vfx_swapper": 678, "Healthbars": 16, "scores": 872, "NumericUI": 14,
}

# Mods that override other mods' effects and so must load after everything.
# New mods are inserted above these. Overridable via "load_last" in state.
DEFAULT_LOAD_LAST = ["vfx_swapper"]

# Files an update never needs to carry over from the old folder: code and
# manifests are the author's to replace. Anything else left behind (a
# config the user edited, a saved preset) is copied into the new version.
CODE_SUFFIXES = {".lua", ".mod", ".json", ".md", ".txt"}

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# Paths and state
# --------------------------------------------------------------------------

@dataclass
class Paths:
    game: Path
    downloads: Path
    logs: Path

    @property
    def mods(self) -> Path:
        return self.game / "mods"

    @property
    def load_order(self) -> Path:
        return self.mods / "mod_load_order.txt"

    @property
    def patcher(self) -> Path:
        return self.game / "tools" / "dtkit-patch.exe"

    @property
    def manifest(self) -> Path:
        # <library>/steamapps/common/<game> -> <library>/steamapps/appmanifest
        return self.game.parent.parent / f"appmanifest_{DARKTIDE_APP_ID}.acf"

    @property
    def crash_dumps(self) -> Path:
        return self.logs.parent / "crash_dumps"


def steam_library_game_dirs() -> list[Path]:
    """Darktide candidates from every Steam library, default one first."""
    found = [DEFAULT_GAME_DIR]
    vdf = Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf")
    try:
        for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text("utf-8")):
            found.append(Path(lib.replace("\\\\", "\\")) / "steamapps" /
                         "common" / "Warhammer 40,000 DARKTIDE")
    except OSError:
        pass
    return found


def default_paths(state: dict) -> Paths:
    game = None
    for cand in [Path(state["game_dir"])] if state.get("game_dir") else []:
        if (cand / "mods").is_dir():
            game = cand
    if game is None:
        game = next((c for c in steam_library_game_dirs()
                     if (c / "mods").is_dir()), DEFAULT_GAME_DIR)
    return Paths(
        game=game,
        downloads=Path(state.get("downloads_dir") or Path.home() / "Downloads"),
        logs=Path(os.environ.get("APPDATA", "")) / "Fatshark" / "Darktide" /
        "console_logs",
    )


def load_state(path: Path | None = None) -> dict:
    # Looked up at call time, not bound as a default, so tests can
    # redirect STATE_PATH without writing to the real state file.
    path = path or STATE_PATH
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), "utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

def version_key(v: str | None) -> tuple:
    """Sortable key for the free-form versions Darktide authors use.

    Seen in the wild: "1.3", "1.2.7", "26.09.29.1", "6.5.0b", "1.02". Numbers
    compare as numbers, so "1.02" equals "1.2" and "26.09.29.1" beats
    "26.09.29". A letter suffix sorts below the next number.
    """
    if not v:
        return ()
    return tuple((1, int(p)) if p.isdigit() else (0, p)
                 for p in re.findall(r"\d+|[a-z]+", v.lower()))


def is_newer(candidate: str | None, installed: str | None) -> bool:
    if not candidate or not installed:
        return False
    return version_key(candidate) > version_key(installed)


# --------------------------------------------------------------------------
# Installed mods
# --------------------------------------------------------------------------

@dataclass
class LocalMod:
    folder: str
    version: str | None = None
    version_source: str = ""       # "recorded", "info.json", ".mod" or ""
    nexus_id: int | None = None
    enabled: bool | None = None    # None = folder exists, not in load order
    id_source: str = ""            # how nexus_id was found; see resolve_nexus_id
    custom: bool = False           # user said "not on Nexus"


def stamp_file(folder: Path) -> Path | None:
    """The file whose mtime identifies one particular install of a mod.

    A version recorded by this app is only trusted while this file is the
    one it installed. Updating a mod by hand replaces it, and the stale
    record is then ignored instead of reporting the old version.
    """
    for pattern in ("*.mod", "info.json", "mod_manager.lua"):
        hit = sorted(folder.glob(pattern))
        if hit:
            return hit[0]
    return None


def folder_stamp(folder: Path) -> int | None:
    f = stamp_file(folder)
    try:
        return f.stat().st_mtime_ns if f else None
    except OSError:
        return None


def read_metadata(folder: Path) -> tuple[str | None, str, int | None]:
    """(version, where it came from, nexus id) from the mod's own files.

    info.json beats the .mod manifest: authors bump info.json with the
    release and routinely forget the .mod (vfx_swapper 1.2.7 and
    Enhanced_descriptions 6.5.0b both still declare older versions there).
    """
    version, source, nexus_id = None, "", None
    try:
        info = json.loads((folder / "info.json").read_text("utf-8-sig"))
        version = info.get("version") or None
        source = "info.json" if version else ""
        m = re.search(r"/mods/(\d+)", str(info.get("homepage", "")))
        nexus_id = int(m.group(1)) if m else None
    except (OSError, ValueError, AttributeError):
        pass
    if version is None:
        for mod_file in sorted(folder.glob("*.mod")):
            try:
                m = re.search(r'version\s*=\s*"([^"]+)"',
                              mod_file.read_text("utf-8", errors="replace"))
            except OSError:
                continue
            if m:
                version, source = m.group(1), ".mod"
                break
    return version, source, nexus_id


def recorded_version(state: dict, folder: str, stamp: int | None) -> str | None:
    for entry in state.get("installed", {}).get(folder, []):
        if stamp is not None and entry.get("stamp") == stamp:
            return entry.get("version")
    return None


def record_version(state: dict, folder: str, version: str,
                   stamp: int | None) -> None:
    history = state.setdefault("installed", {}).setdefault(folder, [])
    history[:] = [e for e in history if e.get("stamp") != stamp]
    history.insert(0, {"version": version, "stamp": stamp,
                       "at": dt.datetime.now().isoformat(timespec="seconds")})
    del history[10:]


def resolve_nexus_id(folder: Path, meta_id: int | None, state: dict,
                     catalog_index: dict | None) -> tuple[int | None, str]:
    """(Nexus id, how it was found), most trustworthy source first.

    Only newer mods say where they came from (info.json), so for everything
    else the id is inferred: from a user's own link, from a Nexus zip in
    Downloads that contains this folder (the zip name carries the id), from
    a short built-in list, and last from matching names against the Nexus
    catalog. A stored id of 0 means the user marked the mod as not on Nexus.
    """
    name = folder.name
    if meta_id:
        return meta_id, "info.json"
    linked = state.get("nexus_ids", {}).get(name)
    if linked is not None:
        return (linked, "linked") if linked else (None, "custom")
    for zip_name, zip_folder in state.get("zip_index", {}).items():
        if zip_folder == name:
            parsed = parse_download(zip_name)
            if parsed:
                return parsed[0], "zip in Downloads"
    if name in KNOWN_NEXUS_IDS:
        return KNOWN_NEXUS_IDS[name], "built-in list"
    if catalog_index:
        hit = match_by_name(folder, catalog_index)
        if hit:
            return hit[0], f"name match: {hit[1]}"
    return None, ""


def scan_mods(paths: Paths, state: dict,
              catalog_index: dict | None = None) -> list[LocalMod]:
    folders = sorted((p for p in paths.mods.iterdir() if p.is_dir()),
                     key=lambda p: (p.name not in FRAMEWORK, p.name.lower()))
    status = load_order_status(read_load_order(paths.load_order),
                               {p.name for p in folders})
    mods = []
    for p in folders:
        meta_version, source, meta_id = read_metadata(p)
        rec = recorded_version(state, p.name, folder_stamp(p))
        nexus_id, id_source = resolve_nexus_id(p, meta_id, state, catalog_index)
        mods.append(LocalMod(
            folder=p.name,
            version=rec or meta_version,
            version_source="recorded" if rec else source,
            nexus_id=nexus_id,
            enabled=True if p.name in FRAMEWORK else status.get(p.name),
            id_source=id_source,
            custom=(id_source == "custom"),
        ))
    return mods


# --------------------------------------------------------------------------
# Finding Nexus ids for mods that don't declare one
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def name_keys(nexus_name: str) -> set[str]:
    """Ways a Nexus title might match a folder name.

    Titles carry sales copy the folder never has: "Who Are You - Display
    account names", "Enemies Improved (Healthbars - Debuffs - Outlines and
    more)", "Alf's DMF (Mod Settings) Extensions". So also try the part
    before " - " and the title with bracketed asides removed.
    """
    keys = {_norm(nexus_name)}
    no_brackets = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", nexus_name)
    keys.add(_norm(no_brackets))
    for variant in (nexus_name, no_brackets):
        keys.add(_norm(variant.split(" - ")[0]))
        keys.add(_norm(variant.split(": ")[0]))
    return {k for k in keys if len(k) >= 3}


def build_catalog_index(catalog: list[dict]) -> dict[str, list[tuple[int, str]]]:
    index: dict[str, list[tuple[int, str]]] = {}
    for m in catalog:
        for k in name_keys(m["name"]):
            index.setdefault(k, []).append((m["modId"], m["name"]))
    return index


def local_names(folder: Path) -> list[str]:
    """The folder name, plus the display name from its localization file."""
    names = [folder.name]
    for loc in folder.glob("scripts/mods/*/*localization*.lua"):
        try:
            text = loc.read_text("utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'mod_name\s*=\s*\{\s*en\s*=\s*"([^"]+)"', text)
        if m:
            names.append(m.group(1))
            break
    return names


def match_by_name(folder: Path,
                  index: dict[str, list[tuple[int, str]]]) -> tuple[int, str] | None:
    """A unique catalog match for this mod, or None when unsure.

    Ambiguity is resolved only by dropping titles marked obsolete (Crosshair
    Remap has an "(OBSOLETE)" original and a "(Continued)" fork); anything
    still ambiguous is left for the user to link by hand, since a wrong
    guess would offer the wrong mod's updates.
    """
    hits: dict[int, str] = {}
    exact: dict[int, str] = {}
    for n in local_names(folder):
        for k in name_keys(n):
            for mod_id, title in index.get(k, []):
                hits[mod_id] = title
                # "Scoreboard" is a whole-title match; "Scoreboard (ko-kr)",
                # a translation, only matches once its brackets are cut.
                if _norm(title) == k:
                    exact[mod_id] = title
    if len(hits) > 1:
        hits = {i: t for i, t in hits.items()
                if not re.search(r"obsolete|deprecated|outdated", t, re.I)}
    if len(hits) > 1 and len(exact) == 1:
        hits = exact
    if len(hits) == 1:
        return next(iter(hits.items()))
    return None


def index_download_zips(downloads: Path, state: dict) -> bool:
    """Record which mod folder each Nexus zip in Downloads contains.

    The zip's name gives the Nexus id and its contents give the folder, so
    a zip left over from a manual install identifies that mod exactly.
    Each zip is opened once; results are cached in state. Returns True if
    the cache changed.
    """
    cache = state.setdefault("zip_index", {})
    changed = False
    try:
        entries = [p for p in downloads.iterdir()
                   if p.suffix.lower() == ".zip" and p.name not in cache
                   and parse_download(p.name)]
    except OSError:
        return False
    for p in entries:
        folder = ""
        try:
            with zipfile.ZipFile(p) as zf:
                for n in zf.namelist():
                    parts = n.replace("\\", "/").split("/")
                    if len(parts) == 2 and parts[1].endswith(".mod"):
                        folder = parts[0]
                        break
        except (OSError, zipfile.BadZipFile):
            pass
        cache[p.name] = folder
        changed = True
    return changed


# --------------------------------------------------------------------------
# Load order
# --------------------------------------------------------------------------
# DMF skips any line starting with "--", so a mod is disabled by commenting
# its line out, never by deleting it: its position survives, and turning it
# back on is the same edit in reverse.

def read_load_order(path: Path) -> list[str]:
    try:
        return path.read_text("utf-8").splitlines()
    except OSError:
        return []


def _line_mod(line: str, folders: set[str]) -> tuple[str | None, bool]:
    """(mod folder named on this line, enabled?). Header comments -> None."""
    s = line.strip()
    if not s:
        return None, False
    if s.startswith("--"):
        words = s[2:].split()
        # "-- scores  -- disabled ..." is a mod; "-- Enter user mod names"
        # is the loader's header. The name must be a real folder, and only
        # a "--" note may follow it.
        if (words and words[0] in folders
                and (len(words) == 1 or words[1].startswith("--"))):
            return words[0], False
        return None, False
    return s, True


def load_order_status(lines: list[str], folders: set[str]) -> dict[str, bool]:
    status: dict[str, bool] = {}
    for line in lines:
        name, enabled = _line_mod(line, folders)
        if name and (enabled or name not in status):
            status[name] = enabled
    return status


def set_enabled(lines: list[str], folder: str, enabled: bool,
                folders: set[str], load_last: list[str],
                today: str | None = None) -> list[str]:
    today = today or dt.date.today().isoformat()
    folders = folders | {folder}
    out = list(lines)
    for i, line in enumerate(out):
        name, is_on = _line_mod(line, folders)
        if name != folder:
            continue
        if is_on and not enabled:
            out[i] = f"-- {folder}  -- disabled {today}"
        elif not is_on and enabled:
            out[i] = folder
        return out
    if not enabled:
        return out
    # New to the list: load it before the mods pinned to load last (an
    # effect-override mod like vfx_swapper must not have its swaps undone
    # by something loading after it).
    for i, line in enumerate(out):
        name, is_on = _line_mod(line, folders)
        if is_on and name in load_last and folder not in load_last:
            out.insert(i, folder)
            return out
    out.append(folder)
    return out


def write_load_order(paths: Paths, lines: list[str]) -> None:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / "load_order" / f"mod_load_order.{stamp}.txt"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if paths.load_order.exists():
        shutil.copy2(paths.load_order, backup)
    paths.load_order.write_text("\n".join(lines) + "\n", "utf-8")


def toggle_mod(paths: Paths, state: dict, folder: str, enabled: bool) -> None:
    folders = {p.name for p in paths.mods.iterdir() if p.is_dir()}
    lines = read_load_order(paths.load_order)
    new = set_enabled(lines, folder, enabled, folders,
                      state.get("load_last", DEFAULT_LOAD_LAST))
    if new != lines:
        write_load_order(paths, new)


# --------------------------------------------------------------------------
# Nexus
# --------------------------------------------------------------------------

class NexusError(Exception):
    pass


@dataclass
class NexusFile:
    file_id: int
    name: str
    version: str
    date: int
    category: str
    changelog: list[str] = field(default_factory=list)


@dataclass
class RemoteMod:
    nexus_id: int
    files: list[NexusFile]

    @property
    def latest(self) -> NexusFile | None:
        # MAIN is the author's current release. OLD_VERSION and ARCHIVED
        # are history; OPTIONAL files are variants, never "the" update.
        main = [f for f in self.files if f.category == "MAIN"]
        pool = main or [f for f in self.files
                        if f.category not in ("ARCHIVED", "DELETED")]
        return max(pool, key=lambda f: f.date) if pool else None

    def newer_than(self, version: str | None) -> list[NexusFile]:
        """Releases after `version`, newest first, one per version."""
        seen, out = set(), []
        for f in sorted(self.files, key=lambda f: -f.date):
            if f.category == "OPTIONAL" or f.version in seen:
                continue
            if version is None or is_newer(f.version, version):
                seen.add(f.version)
                out.append(f)
        return out


def graphql(query: str, timeout: float = 20) -> dict:
    req = urllib.request.Request(
        GRAPHQL_URL, data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NexusError(f"Nexus unreachable: {e}") from e
    if not data.get("data"):
        raise NexusError(f"Nexus error: {data.get('errors')}")
    return data["data"]


def fetch_mod(nexus_id: int) -> RemoteMod:
    # nexus_id is an int we built, never user text, so inlining it is safe.
    data = graphql(
        f"{{ modFiles(modId: {int(nexus_id)}, gameId: {NEXUS_GAME_ID}) "
        "{ fileId name version date category changelogText } }")
    files = [NexusFile(file_id=f["fileId"], name=f.get("name") or "",
                       version=(f.get("version") or "").strip(),
                       date=int(f.get("date") or 0),
                       category=f.get("category") or "",
                       changelog=[str(x) for x in (f.get("changelogText") or [])])
             for f in data.get("modFiles") or []]
    return RemoteMod(nexus_id=nexus_id, files=files)


CATALOG_PAGE = 80          # the API's page size cap; asking for more returns 80
CATALOG_MAX_AGE = 3 * 86400


def fetch_catalog() -> list[dict]:
    """Every Darktide mod on Nexus as {modId, name} (~1,100 in 2026)."""
    out: list[dict] = []
    offset = 0
    while True:
        data = graphql(
            f'{{ mods(filter: {{gameId: [{{value: "{NEXUS_GAME_ID}"}}]}}, '
            f"count: {CATALOG_PAGE}, offset: {offset}) "
            "{ totalCount nodes { modId name } } }")["mods"]
        out += [{"modId": n["modId"], "name": n["name"]} for n in data["nodes"]]
        offset += CATALOG_PAGE
        if not data["nodes"] or offset >= data["totalCount"] or offset > 20000:
            return out


def load_catalog(allow_fetch: bool = True) -> list[dict]:
    """The catalog, cached on disk for a few days; [] if unavailable."""
    path = DATA_DIR / "nexus_catalog.json"
    try:
        cached = json.loads(path.read_text("utf-8"))
        fresh = (dt.datetime.now().timestamp() - cached["at"]) < CATALOG_MAX_AGE
        if fresh or not allow_fetch:
            return cached["mods"]
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    if not allow_fetch:
        return []
    try:
        mods = fetch_catalog()
    except NexusError:
        return cached["mods"] if cached else []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": dt.datetime.now().timestamp(),
                                "mods": mods}), "utf-8")
    return mods


def fetch_all(ids: list[int]) -> tuple[dict[int, RemoteMod], dict[int, str]]:
    remote, errors = {}, {}
    for i in ids:
        try:
            remote[i] = fetch_mod(i)
        except NexusError as e:
            errors[i] = str(e)
    return remote, errors


def file_page_url(nexus_id: int, file_id: int | None = None) -> str:
    url = f"https://www.nexusmods.com/{NEXUS_DOMAIN}/mods/{nexus_id}?tab=files"
    return url + (f"&file_id={file_id}" if file_id else "")


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------

# "Vfx Swapper 678 1.3 2026-09-29T20-52Z RZfXOZuui.zip". The name is matched
# lazily so a name that itself contains a version ("Vfx Swapper 1.2.7 678
# 1.2.7 ...") still finds the id: "1.2.7" is not all digits.
NEXUS_ZIP_RE = re.compile(
    r"^(?P<name>.+?) (?P<id>\d+) (?P<ver>\S+) "
    r"(?P<stamp>\d{4}-\d\d-\d\dT\d\d-\d\dZ) \S+\.zip$", re.I)
# The older Nexus naming, still what the API's "uri" field reports:
# "Enemies Improved-809-1-3-01-1776381380.zip".
LEGACY_ZIP_RE = re.compile(
    r"^(?P<name>.+?)-(?P<id>\d+)-(?P<ver>[0-9a-z]+(?:-[0-9a-z]+)*?)-"
    r"(?P<ts>\d{9,11})\.zip$", re.I)


@dataclass
class Download:
    path: Path
    nexus_id: int
    version: str
    stamp: str


def parse_download(name: str) -> tuple[int, str, str] | None:
    m = NEXUS_ZIP_RE.match(name)
    if m:
        return int(m["id"]), m["ver"], m["stamp"]
    m = LEGACY_ZIP_RE.match(name)
    if m:
        stamp = dt.datetime.fromtimestamp(int(m["ts"]), dt.timezone.utc)
        return (int(m["id"]), m["ver"].replace("-", "."),
                stamp.strftime("%Y-%m-%dT%H-%MZ"))
    return None


# A zip touched more recently than this may still be being written; most
# browsers download to a temp name and rename, but not every one does.
SETTLE_SECONDS = 5


def scan_downloads(folder: Path) -> dict[int, Download]:
    """Newest zip per Nexus id. Old-version zips sit next to new ones."""
    best: dict[int, Download] = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return best
    now = dt.datetime.now().timestamp()
    for p in entries:
        if not p.is_file():
            continue
        parsed = parse_download(p.name)
        if not parsed:
            continue
        try:
            if now - p.stat().st_mtime < SETTLE_SECONDS:
                continue
        except OSError:
            continue
        mod_id, version, stamp = parsed
        cur = best.get(mod_id)
        if cur is None or (version_key(version), stamp) > (
                version_key(cur.version), cur.stamp):
            best[mod_id] = Download(p, mod_id, version, stamp)
    return best


def baseline_downloads(state: dict, folder: Path) -> bool:
    """On first run, count every Nexus zip already in Downloads as seen.

    New-mod auto-install must only act on zips that arrive while the app is
    in use. Downloads is full of zips for mods that were tried and removed
    on purpose; installing those again would undo the user's choice.
    Returns True if the baseline was just taken.
    """
    if "seen_downloads" in state:
        return False
    try:
        state["seen_downloads"] = sorted(
            p.name for p in folder.iterdir() if parse_download(p.name))
    except OSError:
        state["seen_downloads"] = []
    return True


def mark_seen(state: dict, zip_name: str) -> None:
    seen = state.setdefault("seen_downloads", [])
    if zip_name not in seen:
        seen.append(zip_name)


def new_mod_downloads(mods: list[LocalMod], downloads: dict[int, Download],
                      state: dict) -> list[Download]:
    """Zips for mods that aren't installed, which arrived after the baseline.

    Skips a zip whose mod folder already exists (an installed mod the app
    hadn't matched to Nexus yet - that's an update, not a new mod) and one
    with no mod folder inside, such as the mod loader's own zip.
    """
    installed_ids = {m.nexus_id for m in mods if m.nexus_id}
    folders = {m.folder for m in mods}
    seen = set(state.get("seen_downloads", []))
    zip_index = state.get("zip_index", {})
    out = []
    for d in downloads.values():
        if d.nexus_id in installed_ids or d.path.name in seen:
            continue
        folder = zip_index.get(d.path.name)
        if not folder or folder in folders:
            continue
        out.append(d)
    return sorted(out, key=lambda d: d.path.name.lower())


def pending_installs(mods: list[LocalMod],
                     downloads: dict[int, Download]) -> list[tuple[LocalMod, Download]]:
    """Installed mods with a strictly newer zip waiting in Downloads."""
    out = []
    for m in mods:
        d = downloads.get(m.nexus_id) if m.nexus_id else None
        if d and is_newer(d.version, m.version):
            out.append((m, d))
    return out


# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------

class InstallError(Exception):
    pass


def game_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Darktide.exe", "/NH"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
            timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "darktide.exe" in out.lower()


def _safe_extract(zpath: Path, dest: Path) -> None:
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise InstallError(f"{zpath.name}: unsafe path {info.filename}")
        zf.extractall(dest)


def _locate(root: Path) -> tuple[str, Path]:
    """("mod", folder) for an ordinary mod zip, ("root", root) for DML.

    Mod zips wrap one folder holding a .mod file. The loader's zip is laid
    out like the game directory instead (mods/base, tools/, the toggle .bat)
    and has to be copied over the game root.
    """
    if (root / "mods" / "base").is_dir():
        return "root", root
    if list(root.glob("*.mod")):
        raise InstallError("zip has no wrapping folder; install it by hand")
    hits = [p for p in root.iterdir() if p.is_dir() and list(p.glob("*.mod"))]
    if len(hits) != 1:
        raise InstallError(
            f"expected one mod folder in the zip, found {[p.name for p in hits]}")
    return "mod", hits[0]


def backup_slot() -> Path:
    slot = BACKUP_DIR / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    slot.mkdir(parents=True)
    return slot


def install_zip(paths: Paths, state: dict, zpath: Path, version: str,
                nexus_id: int | None, log=print,
                check_running: bool = True) -> str:
    """Install one zip. Returns the folder name. Old folder goes to backups."""
    if check_running and game_running():
        raise InstallError("Darktide is running; close it first")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATA_DIR, prefix="unzip-") as tmp:
        tmp = Path(tmp)
        _safe_extract(zpath, tmp)
        kind, src = _locate(tmp)
        if kind == "root":
            return _install_loader(paths, state, src, version, log)

        folder = src.name
        # A zip whose folder is named differently from the installed one
        # would leave the load order pointing at the old name.
        if nexus_id:
            for m in scan_mods(paths, state):
                if m.nexus_id == nexus_id and m.folder != folder:
                    raise InstallError(
                        f"zip contains '{folder}' but this mod is installed as "
                        f"'{m.folder}'; install it by hand")
        dest = paths.mods / folder
        is_new = not dest.exists()
        kept = []
        if dest.exists():
            slot = backup_slot()
            shutil.move(str(dest), str(slot / folder))
            old = slot / folder
            shutil.move(str(src), str(dest))
            for f in old.rglob("*"):
                rel = f.relative_to(old)
                if (f.is_file() and not (dest / rel).exists()
                        and f.suffix.lower() not in CODE_SUFFIXES):
                    (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest / rel)
                    kept.append(str(rel))
            log(f"{folder}: old version backed up to {slot.name}")
        else:
            shutil.move(str(src), str(dest))
        if kept:
            log(f"{folder}: carried over {', '.join(kept)}")

    record_version(state, folder, version, folder_stamp(dest))
    mark_seen(state, zpath.name)
    if nexus_id and read_metadata(dest)[2] is None:
        state.setdefault("nexus_ids", {})[folder] = nexus_id
    save_state(state)
    # Only a brand-new mod joins the load order. An update to a mod that is
    # off, or was deliberately left out of the list, must not turn it on.
    if is_new and folder not in FRAMEWORK:
        folders = {p.name for p in paths.mods.iterdir() if p.is_dir()}
        status = load_order_status(read_load_order(paths.load_order), folders)
        if folder not in status:
            toggle_mod(paths, state, folder, True)
            log(f"{folder}: added to the load order")
    log(f"{folder}: installed {version}")
    return folder


def _install_loader(paths: Paths, state: dict, src: Path, version: str,
                    log) -> str:
    slot = backup_slot() / "_game_root"
    for top in src.iterdir():
        existing = paths.game / top.name
        if top.name == "mods":
            for sub in top.iterdir():
                if (paths.mods / sub.name).exists() and sub.is_dir():
                    shutil.copytree(paths.mods / sub.name,
                                    slot / "mods" / sub.name)
        elif existing.exists():
            copy = shutil.copytree if existing.is_dir() else shutil.copy2
            slot.mkdir(parents=True, exist_ok=True)
            copy(existing, slot / top.name)
    # The user's load order must survive a loader update.
    lo = src / "mods" / "mod_load_order.txt"
    if lo.exists() and paths.load_order.exists():
        lo.unlink()
    shutil.copytree(src, paths.game, dirs_exist_ok=True)
    record_version(state, "base", version, folder_stamp(paths.mods / "base"))
    save_state(state)
    log(f"Mod loader: installed {version} (old files in {slot.parent.name})")
    ok, out = patch_game(paths)
    log(f"Mod loader: {out}")
    return "base"


def list_backups(folder: str) -> list[Path]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted((p / folder for p in BACKUP_DIR.iterdir()
                   if (p / folder).is_dir()), reverse=True)


def rollback(paths: Paths, state: dict, folder: str, log=print) -> None:
    if game_running():
        raise InstallError("Darktide is running; close it first")
    backups = list_backups(folder)
    if not backups:
        raise InstallError(f"no backup of {folder}")
    dest = paths.mods / folder
    if dest.exists():
        slot = backup_slot()
        shutil.move(str(dest), str(slot / folder))
    shutil.move(str(backups[0]), str(dest))
    try:
        backups[0].parent.rmdir()
    except OSError:
        pass
    log(f"{folder}: rolled back to the copy from {backups[0].parent.name}")


# --------------------------------------------------------------------------
# Game: patching, launching, build
# --------------------------------------------------------------------------

def game_build(paths: Paths) -> str | None:
    try:
        m = re.search(r'"buildid"\s+"(\d+)"',
                      paths.manifest.read_text("utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


def patch_game(paths: Paths) -> tuple[bool, str]:
    """Re-apply the loader patch Steam discards with every game update.

    dtkit-patch --patch is idempotent ("already patched", exit 0), so this
    runs before every launch rather than trying to detect the patch state.
    """
    if not paths.patcher.exists():
        return False, f"patcher not found at {paths.patcher}"
    try:
        r = subprocess.run([str(paths.patcher), "--patch",
                            str(paths.game / "bundle")],
                           cwd=paths.game, capture_output=True, text=True,
                           creationflags=NO_WINDOW, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"patcher failed: {e}"
    out = (r.stdout + r.stderr).strip().replace('"', "")
    return r.returncode == 0, out or f"patcher exit {r.returncode}"


def launch_game() -> None:
    os.startfile(f"steam://rungameid/{DARKTIDE_APP_ID}")


# --------------------------------------------------------------------------
# Crash report
# --------------------------------------------------------------------------

@dataclass
class LuaError:
    message: str
    mods_in_stack: list[str]

    @property
    def suspect(self) -> str | None:
        return self.mods_in_stack[0] if self.mods_in_stack else None


@dataclass
class LogReport:
    log: Path | None
    crashed: bool = False
    game_version: str | None = None
    modded: bool | None = None
    errors: list[LuaError] = field(default_factory=list)
    mod_errors: dict[str, list[str]] = field(default_factory=dict)


def _stack_mods(stack: str) -> list[str]:
    out = []
    for name in re.findall(r"mods/([^/\s]+)/", stack.replace("\\", "/")):
        # DMF's hook dispatcher sits in every modded stack; it only ever
        # relays the call, so it is never the culprit worth naming.
        if name not in FRAMEWORK and name not in out:
            out.append(name)
    return out


def analyse_log(text: str) -> LogReport:
    rep = LogReport(log=None)
    m = re.search(r"game_version = ([^<\s]+)", text)
    rep.game_version = m.group(1) if m else None
    m = re.search(r"is_modded = (true|false)", text)
    rep.modded = (m.group(1) == "true") if m else None

    # A crash is logged as a <<Lua Error>>, then again, much later, as a
    # <<Script Error>> - and only the second copy is followed by the
    # <<Lua Stack>>. So walk the tags in order, hand each stack to every
    # message still waiting for one, and merge repeats of a message.
    by_msg: dict[str, LuaError] = {}
    waiting: list[str] = []
    tags = re.compile(r"<<(?:Lua|Script) Error>>(.*?)<</(?:Lua|Script) Error>>"
                      r"|<<Lua Stack>>(.*?)<</Lua Stack>>", re.S)
    for msg, stack in tags.findall(text):
        if msg:
            msg = msg.strip()
            if msg not in by_msg:
                by_msg[msg] = LuaError(msg, _stack_mods(msg))
            if msg not in waiting:
                waiting.append(msg)
        else:
            for w in waiting:
                err = by_msg[w]
                err.mods_in_stack += [m for m in _stack_mods(stack)
                                      if m not in err.mods_in_stack]
            waiting = []
    rep.errors = list(by_msg.values())

    for mod, msg in re.findall(r"\[MOD\]\[([^\]]+)\]\[ERROR\]\s*(.*)", text):
        bucket = rep.mod_errors.setdefault(mod, [])
        if msg.strip() not in bucket:
            bucket.append(msg.strip())
    return rep


def latest_report(paths: Paths) -> LogReport:
    try:
        logs = sorted(paths.logs.glob("console-*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        logs = []
    if not logs:
        return LogReport(log=None)
    log = logs[0]
    rep = analyse_log(log.read_text("utf-8", errors="replace"))
    rep.log = log
    dump = paths.crash_dumps / (
        log.name.replace("console-", "crash_dump-", 1)[:-4] + ".dmp")
    rep.crashed = dump.exists()
    return rep


def format_report(rep: LogReport) -> str:
    if rep.log is None:
        return "No Darktide console log found."
    lines = [f"Log: {rep.log.name}",
             f"Game {rep.game_version or '?'}, "
             f"{'modded' if rep.modded else 'NOT modded (re-patch?)' if rep.modded is False else 'mod state unknown'}, "
             f"{'CRASHED' if rep.crashed else 'no crash dump'}"]
    if not rep.errors and not rep.mod_errors:
        lines.append("\nNo Lua errors in this session.")
    for e in rep.errors:
        who = (f"Suspect: {e.suspect}" if e.suspect
               else "No mod in the stack (the game itself, or a hook DMF hid)")
        lines += ["", who, f"  {e.message}"]
        if len(e.mods_in_stack) > 1:
            lines.append(f"  Also in the stack: {', '.join(e.mods_in_stack[1:])}")
    if rep.mod_errors:
        lines.append("\nErrors mods logged through DMF:")
        for mod, msgs in rep.mod_errors.items():
            for msg in msgs[:5]:
                lines.append(f"  {mod}: {msg}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk

    state = load_state()
    paths = default_paths(state)

    root = tk.Tk()
    root.title("Darktide Mods")
    root.geometry("1060x680")
    root.minsize(820, 520)

    # First run on a machine where Darktide is not in the default Steam
    # library: ask once, remember it.
    while not paths.mods.is_dir():
        if not messagebox.askokcancel(
                "Darktide Mods",
                "Couldn't find Darktide's mods folder.\n\nPick your "
                "'Warhammer 40,000 DARKTIDE' game folder. Darktide Mod Loader "
                "must already be installed there."):
            root.destroy()
            return
        chosen = filedialog.askdirectory(title="Darktide game folder")
        if chosen:
            state["game_dir"] = chosen
            save_state(state)
            paths = default_paths(state)

    remote: dict[int, RemoteMod] = {}
    # Cached catalog only at startup; the update check refreshes it off the
    # UI thread, so opening the window never waits on 14 Nexus requests.
    catalog_index = build_catalog_index(load_catalog(allow_fetch=False))
    mods: list[LocalMod] = []
    downloads: dict[int, Download] = {}
    events: queue.Queue = queue.Queue()
    busy = tk.BooleanVar(value=False)
    waiting_for_game = False
    failed_zips: set[Path] = set()
    auto_install = tk.BooleanVar(value=state.get("auto_install", True))
    auto_new = tk.BooleanVar(value=state.get("auto_install_new", True))
    if baseline_downloads(state, paths.downloads):
        save_state(state)

    # ---- layout
    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill="x")
    banner = ttk.Label(top, text="", font=("Segoe UI", 10, "bold"))
    banner.pack(side="left")
    ttk.Checkbutton(top, text="New mods too", variable=auto_new,
                    command=lambda: (state.__setitem__("auto_install_new",
                                                       auto_new.get()),
                                     save_state(state))).pack(side="right")
    ttk.Checkbutton(top, text="Auto-install downloads",
                    variable=auto_install,
                    command=lambda: (state.__setitem__("auto_install",
                                                       auto_install.get()),
                                     save_state(state))).pack(side="right",
                                                              padx=(0, 8))

    bar = ttk.Frame(root, padding=(10, 0))
    bar.pack(fill="x")

    body = ttk.PanedWindow(root, orient="vertical")
    body.pack(fill="both", expand=True, padx=10, pady=8)

    cols = ("on", "mod", "installed", "latest", "released", "status")
    tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="extended")
    for c, text, w, anchor in [
            ("on", "On", 40, "center"), ("mod", "Mod", 230, "w"),
            ("installed", "Installed", 110, "w"), ("latest", "Latest", 110, "w"),
            ("released", "Released", 100, "w"), ("status", "Status", 330, "w")]:
        tree.heading(c, text=text)
        tree.column(c, width=w, anchor=anchor, stretch=(c == "status"))
    tree.tag_configure("update", foreground="#b35c00")
    tree.tag_configure("ready", foreground="#1b7f2a")
    tree.tag_configure("off", foreground="#8a8a8a")
    tree.tag_configure("unknown", foreground="#6d5bd0")
    body.add(tree, weight=3)

    detail = tk.Text(body, height=12, wrap="word", font=("Consolas", 9),
                     relief="flat", padx=8, pady=6)
    body.add(detail, weight=2)

    status_line = ttk.Label(root, text="", padding=(10, 0, 10, 6))
    status_line.pack(fill="x")

    activity: list[str] = []

    def log(msg: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        status_line.config(text=f"{stamp}  {msg}")
        activity.append(f"{stamp}  {msg}")
        del activity[:-200]

    def show(text: str) -> None:
        detail.config(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.config(state="disabled")

    # ---- model -> view
    def row_status(m: LocalMod) -> tuple[str, str]:
        r = remote.get(m.nexus_id) if m.nexus_id else None
        latest = r.latest if r else None
        d = downloads.get(m.nexus_id) if m.nexus_id else None
        if m.enabled is False:
            base, tag = "disabled", "off"
        elif m.enabled is None:
            base, tag = "not in load order", "off"
        else:
            base, tag = "", ""
        if m.custom:
            return (base or "custom (you marked it not on Nexus)"), tag
        if not m.nexus_id:
            return ("not matched to Nexus - select it, then Link to Nexus…"
                    + (f" ({base})" if base else "")), "unknown"
        if d and is_newer(d.version, m.version):
            return f"zip ready: {d.version} in Downloads", "ready"
        if m.version is None:
            return ("version unknown - select and Set version"
                    + (f" ({base})" if base else "")), "unknown"
        if latest and is_newer(latest.version, m.version):
            return (f"UPDATE to {latest.version}" +
                    (f" ({base})" if base else "")), (tag or "update")
        if latest:
            return (base or "up to date"), tag
        return (base or ("checking..." if busy.get() else "not checked")), tag

    def refresh() -> None:
        nonlocal mods, downloads
        if index_download_zips(paths.downloads, state):
            save_state(state)
        try:
            mods = scan_mods(paths, state, catalog_index)
        except OSError as e:
            show(f"Cannot read {paths.mods}: {e}")
            return
        downloads = scan_downloads(paths.downloads)
        selected = set(tree.selection())
        tree.delete(*tree.get_children())
        for m in mods:
            r = remote.get(m.nexus_id) if m.nexus_id else None
            latest = r.latest if r else None
            text, tag = row_status(m)
            on = {True: "✓", False: "", None: "–"}[m.enabled]
            released = (dt.datetime.fromtimestamp(latest.date).strftime("%Y-%m-%d")
                        if latest else "")
            tree.insert("", "end", iid=m.folder, tags=(tag,) if tag else (),
                        values=(on, m.folder, m.version or "?",
                                latest.version if latest else "", released, text))
        tree.selection_set([i for i in selected if tree.exists(i)])
        update_banner()

    def update_banner() -> None:
        build = game_build(paths)
        last = state.get("last_build")
        n_upd = sum(1 for m in mods if row_status(m)[1] == "update")
        n_ready = (len(pending_installs(mods, downloads))
                   + len(new_mod_downloads(mods, downloads, state)))
        parts = []
        if build and last and build != last:
            parts.append(f"Game updated (build {last} → {build}): "
                         "use Patch && Launch to re-enable mods")
        elif build and not last:
            state["last_build"] = build
            save_state(state)
        parts.append(f"{n_upd} update(s) on Nexus" if n_upd else "No updates known")
        if n_ready and waiting_for_game:
            parts.append(f"{n_ready} zip(s) ready - installs when Darktide closes")
        elif n_ready:
            parts.append(f"{n_ready} zip(s) ready - press Install downloads")
        banner.config(text="   ·   ".join(parts),
                      foreground="#b35c00" if (n_upd or n_ready) else "")

    def selected_mods() -> list[LocalMod]:
        sel = set(tree.selection())
        return [m for m in mods if m.folder in sel]

    def on_select(_=None) -> None:
        sel = selected_mods()
        if len(sel) != 1:
            return
        m = sel[0]
        lines = [f"{m.folder}",
                 f"Installed: {m.version or 'unknown'}"
                 + (f" (from {m.version_source})" if m.version_source else "")]
        if m.nexus_id:
            lines.append(f"Nexus: {file_page_url(m.nexus_id).split('?')[0]}"
                         f"  (found via {m.id_source})")
            if m.id_source.startswith("name match"):
                lines.append("  Wrong mod? Use Link to Nexus… to correct it.")
        elif not m.custom:
            lines.append("Not matched to a Nexus mod. Use Link to Nexus… and paste "
                         "its Nexus page address, or leave it empty if it isn't "
                         "on Nexus.")
        b = list_backups(m.folder)
        if b:
            lines.append(f"Backups: {len(b)} (newest {b[0].parent.name[:15]})")
        r = remote.get(m.nexus_id) if m.nexus_id else None
        if r:
            newer = r.newer_than(m.version) if m.version else r.newer_than(None)[:1]
            if newer:
                lines.append("")
                lines.append("Changes since your version:" if m.version
                             else "Latest release:")
                for f in newer:
                    when = dt.datetime.fromtimestamp(f.date).strftime("%Y-%m-%d")
                    lines.append(f"\n{f.version}  ({when})")
                    for c in f.changelog or ["(no changelog)"]:
                        lines.append(f"  • {c}")
        show("\n".join(lines))

    tree.bind("<<TreeviewSelect>>", on_select)

    # ---- background work
    def check_updates() -> None:
        if busy.get():
            return
        busy.set(True)
        log("Checking mods on Nexus...")
        snapshot = json.loads(json.dumps(state))   # the thread's own copy

        def work():
            # The catalog (for name matching) is refreshed first, so mods
            # it newly identifies are included in this same check.
            index = build_catalog_index(load_catalog())
            found = scan_mods(paths, snapshot, index)
            ids = sorted({m.nexus_id for m in found if m.nexus_id})
            events.put(("checked", (*fetch_all(ids), index)))
        threading.Thread(target=work, daemon=True).start()

    def pump() -> None:
        nonlocal catalog_index
        try:
            while True:
                kind, payload = events.get_nowait()
                if kind == "checked":
                    got, errors, index = payload
                    if index:
                        catalog_index = index
                    remote.update(got)
                    busy.set(False)
                    state["last_check"] = dt.datetime.now().isoformat(timespec="seconds")
                    save_state(state)
                    log(f"Checked Nexus: {len(got)} ok"
                        + (f", {len(errors)} failed ({next(iter(errors.values()))})"
                           if errors else ""))
                    refresh()
                    on_select()
        except queue.Empty:
            pass
        root.after(250, pump)

    def watch() -> None:
        # Poll Downloads: a directory listing every few seconds is cheaper
        # than a change-notification thread, and zips arrive at human speed.
        # Auto-install works off "what is pending now", not "what changed
        # since the last poll": refresh() also rescans Downloads, so a zip
        # that arrived just before a refresh would never look like a change.
        nonlocal downloads, waiting_for_game
        fresh = scan_downloads(paths.downloads)
        if ({k: (v.path, v.version) for k, v in fresh.items()} !=
                {k: (v.path, v.version) for k, v in downloads.items()}):
            downloads = fresh
            refresh()
        todo = [t for t in pending_installs(mods, downloads)
                if t[1].path not in failed_zips]
        if auto_install.get() and auto_new.get():
            todo += [(None, d) for d in new_mod_downloads(mods, downloads, state)
                     if d.path not in failed_zips]
        if auto_install.get() and todo:
            running = game_running()
            if running != waiting_for_game:
                waiting_for_game = running
                if running:
                    log(f"{len(todo)} zip(s) ready; will install once Darktide closes")
                update_banner()
            if not running:
                install_many(todo)
        elif waiting_for_game:
            waiting_for_game = False
            update_banner()
        root.after(3000, watch)

    # ---- actions
    def install_many(todo) -> None:
        # m is None for a brand-new mod; it gets added to the load order.
        for m, d in todo:
            label = m.folder if m else d.path.name
            try:
                install_zip(paths, state, d.path, d.version, d.nexus_id, log=log)
            except (InstallError, OSError, zipfile.BadZipFile) as e:
                # Remembered so auto-install doesn't retry it every 3 s.
                failed_zips.add(d.path)
                log(f"{label}: install failed: {e}")
                messagebox.showerror("Install failed", f"{label}: {e}")
        refresh()
        on_select()

    def act_install() -> None:
        # Everything waiting, unless the selection names mods that have a
        # zip: a row left selected from reading a changelog must not
        # silently filter the install down to nothing.
        todo = pending_installs(mods, downloads)
        sel = {m.folder for m in selected_mods()}
        if any(t[0].folder in sel for t in todo):
            todo = [t for t in todo if t[0].folder in sel]
        else:
            todo += [(None, d) for d in new_mod_downloads(mods, downloads, state)]
        if not todo:
            messagebox.showinfo("Nothing to install",
                                "No newer zips for these mods in "
                                f"{paths.downloads}.\n\nUse 'Get update' to open "
                                "the Nexus page, or 'Install zip…' for any zip.")
            return
        if game_running():
            messagebox.showwarning("Darktide is running", "Close the game first.")
            return
        install_many(todo)

    def act_install_file() -> None:
        f = filedialog.askopenfilename(
            title="Install a mod zip", initialdir=str(paths.downloads),
            filetypes=[("Zip archives", "*.zip")])
        if not f:
            return
        parsed = parse_download(Path(f).name)
        version = parsed[1] if parsed else simpledialog.askstring(
            "Version", "Version of this zip (for update tracking):", parent=root)
        if not version:
            return
        try:
            folder = install_zip(paths, state, Path(f), version,
                                 parsed[0] if parsed else None, log=log)
        except (InstallError, OSError, zipfile.BadZipFile) as e:
            messagebox.showerror("Install failed", str(e))
            return
        refresh()
        if tree.exists(folder):
            tree.selection_set(folder)

    def act_get() -> None:
        sel = selected_mods() or [m for m in mods if row_status(m)[1] == "update"]
        opened = 0
        for m in sel:
            if not m.nexus_id:
                continue
            r = remote.get(m.nexus_id)
            latest = r.latest if r else None
            webbrowser.open(file_page_url(m.nexus_id,
                                          latest.file_id if latest else None))
            opened += 1
        log(f"Opened {opened} Nexus page(s); downloads install themselves"
            if auto_install.get() else f"Opened {opened} Nexus page(s)")

    def act_toggle() -> None:
        sel = [m for m in selected_mods() if m.folder not in FRAMEWORK]
        if not sel:
            return
        turn_on = not all(m.enabled for m in sel)
        for m in sel:
            toggle_mod(paths, state, m.folder, turn_on)
        log(f"{'Enabled' if turn_on else 'Disabled'} {', '.join(m.folder for m in sel)}"
            + (" (takes effect next launch)" if game_running() else ""))
        refresh()

    def act_rollback() -> None:
        sel = selected_mods()
        if len(sel) != 1:
            messagebox.showinfo("Roll back", "Select one mod.")
            return
        m = sel[0]
        b = list_backups(m.folder)
        if not b:
            messagebox.showinfo("Roll back", f"No backup of {m.folder}.")
            return
        if not messagebox.askyesno(
                "Roll back", f"Restore {m.folder} from the backup made "
                f"{b[0].parent.name[:15]}?\nThe current copy is backed up too."):
            return
        try:
            rollback(paths, state, m.folder, log=log)
        except (InstallError, OSError) as e:
            messagebox.showerror("Roll back failed", str(e))
        refresh()
        on_select()

    def act_set_version() -> None:
        sel = selected_mods()
        if not sel:
            messagebox.showinfo("Set version", "Select a mod first.")
            return
        if len(sel) > 1:
            # First-run shortcut: many older mods carry no version at all,
            # and on a fresh install they are usually the latest anyway.
            marks = [(m, remote[m.nexus_id].latest) for m in sel
                     if m.nexus_id in remote and remote[m.nexus_id].latest]
            if not marks:
                messagebox.showinfo("Set version", "Check updates first.")
                return
            if not messagebox.askyesno(
                    "Set version",
                    "Record these as the latest Nexus version?\n\n" +
                    "\n".join(f"{m.folder}: {f.version}" for m, f in marks)):
                return
            for m, f in marks:
                record_version(state, m.folder, f.version,
                               folder_stamp(paths.mods / m.folder))
            save_state(state)
            refresh()
            return
        m = sel[0]
        r = remote.get(m.nexus_id) if m.nexus_id else None
        guess = m.version or (r.latest.version if r and r.latest else "")
        v = simpledialog.askstring(
            "Set version", f"Installed version of {m.folder}:",
            initialvalue=guess, parent=root)
        if v:
            record_version(state, m.folder, v.strip(),
                           folder_stamp(paths.mods / m.folder))
            save_state(state)
            refresh()
            on_select()

    def act_link() -> None:
        sel = selected_mods()
        if len(sel) != 1:
            messagebox.showinfo("Link to Nexus", "Select one mod.")
            return
        m = sel[0]
        answer = simpledialog.askstring(
            "Link to Nexus",
            f"Paste the Nexus page address for {m.folder}\n"
            "(e.g. https://www.nexusmods.com/warhammer40kdarktide/mods/14)\n\n"
            "Leave it empty if this mod is not on Nexus.",
            initialvalue=(file_page_url(m.nexus_id).split("?")[0]
                          if m.nexus_id else ""), parent=root)
        if answer is None:
            return
        answer = answer.strip()
        found = re.search(r"/mods/(\d+)", answer) or re.fullmatch(r"(\d+)", answer)
        if answer and not found:
            messagebox.showerror("Link to Nexus",
                                 "That doesn't look like a Nexus mod address.")
            return
        state.setdefault("nexus_ids", {})[m.folder] = int(found.group(1)) if found else 0
        save_state(state)
        log(f"{m.folder}: " + (f"linked to Nexus mod {found.group(1)}" if found
                               else "marked as not on Nexus"))
        refresh()
        if found:
            check_updates()

    def act_crash() -> None:
        tree.selection_set([])
        rep = latest_report(paths)
        show(format_report(rep))

    def act_launch() -> None:
        if game_running():
            messagebox.showinfo("Darktide", "Darktide is already running.")
            return
        ok, out = patch_game(paths)
        log(f"Patch: {out}")
        if not ok:
            messagebox.showerror("Patch failed", out)
            return
        build = game_build(paths)
        if build:
            state["last_build"] = build
            save_state(state)
        launch_game()
        update_banner()

    def act_activity() -> None:
        tree.selection_set([])
        show("\n".join(activity) or "Nothing yet.")

    for text, cmd in [
            ("Check updates", check_updates), ("Get update ↗", act_get),
            ("Install downloads", act_install), ("Install zip…", act_install_file),
            ("Enable / disable", act_toggle), ("Roll back", act_rollback),
            ("Set version…", act_set_version), ("Link to Nexus…", act_link),
            ("Crash report", act_crash),
            ("Activity", act_activity),
            ("Mods folder", lambda: os.startfile(paths.mods))]:
        ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
    ttk.Button(bar, text="Patch && Launch ▶", command=act_launch).pack(side="right")

    refresh()
    act_crash() if state.get("show_crash_on_start") else show(
        "Select a mod for its changelog. 'Crash report' reads the last game log.\n\n"
        f"Game: {paths.game}\nDownloads watched: {paths.downloads}")
    check_updates()
    root.after(250, pump)
    root.after(3000, watch)
    root.mainloop()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="darktide_mods",
                                 description="Darktide mod updater. No command opens the window.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list", help="installed mods and versions")
    sub.add_parser("check", help="compare against Nexus")
    p = sub.add_parser("install", help="install newer zips found in Downloads")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--new", action="store_true",
                   help="also install mods that aren't installed yet")
    sub.add_parser("crash", help="analyse the latest console log")
    sub.add_parser("patch", help="re-apply the mod loader patch")
    p = sub.add_parser("enable", help="enable a mod in the load order")
    p.add_argument("folder")
    p = sub.add_parser("disable", help="comment a mod out of the load order")
    p.add_argument("folder")
    p = sub.add_parser("link", help="set a mod's Nexus id (0 = not on Nexus)")
    p.add_argument("folder")
    p.add_argument("nexus_id", type=int)
    p = sub.add_parser("set-version", help="record the installed version of a mod")
    p.add_argument("folder")
    p.add_argument("version")
    args = ap.parse_args(argv)
    if not args.cmd:
        run_gui()
        return 0

    state = load_state()
    paths = default_paths(state)
    changed = index_download_zips(paths.downloads, state)
    if baseline_downloads(state, paths.downloads) or changed:
        save_state(state)
    catalog = load_catalog(allow_fetch=args.cmd == "check")
    mods = scan_mods(paths, state, build_catalog_index(catalog))

    if args.cmd == "list":
        for m in mods:
            on = {True: "on ", False: "off", None: " - "}[m.enabled]
            print(f"{on} {m.folder:24} {m.version or '?':12} "
                  f"{m.version_source:10} nexus {m.nexus_id or '-':<5} {m.id_source}")
    elif args.cmd == "check":
        remote, errors = fetch_all(sorted({m.nexus_id for m in mods if m.nexus_id}))
        for m in mods:
            r = remote.get(m.nexus_id) if m.nexus_id else None
            latest = r.latest if r else None
            flag = ("UPDATE" if latest and is_newer(latest.version, m.version)
                    else "?" if not m.version and latest else "")
            print(f"{m.folder:24} {m.version or '?':12} -> "
                  f"{latest.version if latest else '-':12} {flag}")
        for i, e in errors.items():
            print(f"nexus {i}: {e}", file=sys.stderr)
    elif args.cmd == "install":
        downloads = scan_downloads(paths.downloads)
        todo = pending_installs(mods, downloads)
        if args.new:
            todo += [(None, d) for d in new_mod_downloads(mods, downloads, state)]
        if not todo:
            print("Nothing to install in", paths.downloads)
        for m, d in todo:
            print(f"{m.folder}: {m.version} -> {d.version}" if m
                  else f"new: {d.path.name}")
            if not args.dry_run:
                install_zip(paths, state, d.path, d.version, d.nexus_id)
    elif args.cmd == "crash":
        print(format_report(latest_report(paths)))
    elif args.cmd == "patch":
        ok, out = patch_game(paths)
        print(out)
        return 0 if ok else 1
    elif args.cmd in ("enable", "disable"):
        toggle_mod(paths, state, args.folder, args.cmd == "enable")
        print(f"{args.folder}: {args.cmd}d")
    elif args.cmd == "link":
        state.setdefault("nexus_ids", {})[args.folder] = args.nexus_id
        save_state(state)
        print(f"{args.folder}: linked to {args.nexus_id or 'nothing (custom)'}")
    elif args.cmd == "set-version":
        record_version(state, args.folder, args.version,
                       folder_stamp(paths.mods / args.folder))
        save_state(state)
        print(f"{args.folder}: recorded {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
