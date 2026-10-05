"""Build the exe and publish it as a GitHub release.

    python release.py            # build, zip, tag v<APP_VERSION>, upload
    python release.py --dry-run  # build and zip only; nothing tagged or uploaded

Bump APP_VERSION in darktide_mods.py and commit before running. The script
refuses a dirty tree or an existing tag, so a release always matches a
commit on GitHub and is never silently overwritten.

Needs the build venv (see README) and an authenticated `gh`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".build-venv" / "Scripts" / "python.exe"
DIST = ROOT / "dist"
APP = "DarktideMods"
DESKTOP = Path.home() / "Desktop"


def run(*cmd: str, capture: bool = False) -> str:
    r = subprocess.run(cmd, cwd=ROOT, text=True,
                       capture_output=capture, check=False)
    if r.returncode != 0:
        sys.exit(f"failed: {' '.join(cmd)}\n{(r.stderr or '') if capture else ''}")
    return (r.stdout or "").strip() if capture else ""


def app_version() -> str:
    m = re.search(r'^APP_VERSION = "([^"]+)"',
                  (ROOT / "darktide_mods.py").read_text("utf-8"), re.M)
    if not m:
        sys.exit("APP_VERSION not found in darktide_mods.py")
    return m.group(1)


def release_notes(tag: str) -> str:
    """Commit subjects since the previous release tag."""
    tags = run("git", "tag", "--list", "v*", "--sort=-v:refname",
               capture=True).split()
    prev = next((t for t in tags if t != tag), None)
    span = f"{prev}..HEAD" if prev else "HEAD"
    log = run("git", "log", span, "--no-merges", "--format=- %s", capture=True)
    return (log or "- Maintenance") + (
        "\n\nDownload the zip below, unzip the whole DarktideMods folder and "
        "run DarktideMods.exe. Your settings carry over from older versions. "
        "See READ ME.txt inside for setup and the Windows Security note.")


def build(version: str) -> Path:
    if not VENV_PY.exists():
        sys.exit("build venv missing - see README 'Packaging for someone else'")
    print("Running tests...")
    run(sys.executable, "-m", "unittest", "-q", "test_darktide_mods")
    print("Building...")
    for d in (DIST, ROOT / "build"):
        shutil.rmtree(d, ignore_errors=True)
    # --onedir, not --onefile: see README for the Defender story.
    run(str(VENV_PY), "-m", "PyInstaller", "--onedir", "--windowed",
        "--name", APP, "--noconfirm", "--clean", "--log-level", "WARN",
        "darktide_mods.py")
    folder = DIST / APP
    shutil.copy2(ROOT / "READ ME.txt", folder / "READ ME.txt")
    zpath = DIST / f"{APP}-v{version}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(folder.rglob("*")):
            if f.is_file():
                zf.write(f, Path(APP) / f.relative_to(folder))
    print(f"Built {zpath.name} ({zpath.stat().st_size / 1e6:.1f} MB)")
    return zpath


def copy_to_desktop(zpath: Path) -> None:
    """Keep the Desktop copies current, as before releases existed."""
    try:
        shutil.copy2(zpath, DESKTOP / f"{APP}.zip")
        target = DESKTOP / APP
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(DIST / APP, target)
        print(f"Updated {DESKTOP / APP} and {APP}.zip on the Desktop")
    except OSError as e:
        # Usually the app is running from the Desktop folder.
        print(f"Desktop copy skipped: {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-desktop", action="store_true")
    args = ap.parse_args()

    version = app_version()
    tag = f"v{version}"
    if not args.dry_run:
        if run("git", "status", "--porcelain", capture=True):
            sys.exit("working tree has uncommitted changes; commit first")
        if run("git", "tag", "--list", tag, capture=True):
            sys.exit(f"{tag} already exists; bump APP_VERSION in darktide_mods.py")

    zpath = build(version)
    if not args.no_desktop:
        copy_to_desktop(zpath)
    if args.dry_run:
        print("Dry run: not tagged or uploaded.")
        return

    run("git", "push", "origin", "HEAD")
    run("git", "tag", "-a", tag, "-m", f"Darktide Mods {version}")
    run("git", "push", "origin", tag)
    run("gh", "release", "create", tag, str(zpath),
        "--title", f"Darktide Mods {version}", "--notes", release_notes(tag))
    print(run("gh", "release", "view", tag, "--json", "url", "--jq", ".url",
              capture=True))


if __name__ == "__main__":
    main()
