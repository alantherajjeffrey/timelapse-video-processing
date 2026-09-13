"""Build the folder of the public GitHub repository from this version folder (0.8). Nothing is pushed.

    python tools/export_public_repo.py [target] [--replace] [--no-git]

The target (default: "public repo/timelapse-video-processing" beside this version folder) receives
every file git tracks here (every file, without git), except installers and checksums, plus
LICENSE, a .gitignore and the .github folder (workflow and issue template from packaging/github).
The privacy scan (tools/scrub_check.py) then runs on the result; if it is clean, a new git
repository with a single commit is made (author: the maintainer's GitHub no-reply identity).
--replace deletes an earlier export first. Publishing steps: docs/PUBLISHING.md.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
AUTHOR = ("alantherajjeffrey", "alantherajjeffrey@users.noreply.github.com")
EXCLUDED_SUFFIXES = {".exe", ".sha256"}
SKIP_DIRS = {".git", ".venv", ".build", "__pycache__", ".pytest_cache", "dist", "build"}


def _writable_then_retry(function, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _version() -> str:
    text = (APP / "source" / "etaluma_video" / "__init__.py").read_text(encoding="utf-8")
    return next(line.split('"')[1] for line in text.splitlines() if line.startswith("VERSION"))


def source_files(app: Path = APP) -> list[Path]:
    """Files to publish, relative to ``app``."""
    try:
        out = subprocess.run(["git", "-C", str(app), "ls-files", "-z", "."], capture_output=True, check=True)
        files = [Path(p) for p in out.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        files = [p.relative_to(app) for p in app.rglob("*")
                 if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(app).parts)]
    return [p for p in files if p.suffix.lower() not in EXCLUDED_SUFFIXES and (app / p).is_file()]


def _scrub(folder: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location("scrub_check", APP / "tools" / "scrub_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan(folder)


def export(target: Path, replace: bool = False, init_git: bool = True) -> Path:
    target = Path(target).resolve()
    if target.exists() and any(target.iterdir()):
        if not replace:
            raise SystemExit(f"{target} is not empty; use --replace to rebuild it")
        if not (target / "source" / "etaluma_video" / "__init__.py").is_file():
            raise SystemExit(f"{target} does not look like an earlier export; not deleting it")
        shutil.rmtree(target, onexc=_writable_then_retry)  # git keeps its objects read-only on Windows
    target.mkdir(parents=True, exist_ok=True)
    for rel in source_files():
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(APP / rel, destination)
    licence = APP / "LICENSE" if (APP / "LICENSE").is_file() else APP.parent / "LICENSE"
    shutil.copy2(licence, target / "LICENSE")
    templates = APP / "packaging" / "github"
    shutil.copy2(templates / "gitignore", target / ".gitignore")
    shutil.copytree(templates / "workflows", target / ".github" / "workflows", dirs_exist_ok=True)
    shutil.copytree(templates / "ISSUE_TEMPLATE", target / ".github" / "ISSUE_TEMPLATE", dirs_exist_ok=True)

    findings = _scrub(target)
    if findings:
        print("\n".join(findings))
        raise SystemExit(f"The privacy scan found {len(findings)} problem(s) in {target}; nothing was committed.")
    if init_git:
        name, email = AUTHOR
        git = ["git", "-C", str(target), "-c", f"user.name={name}", "-c", f"user.email={email}"]
        subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", f"Timelapse Video Processing {_version()}"], check=True)
    return target


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    target = Path(args[0]) if args else APP.parent / "public repo" / "timelapse-video-processing"
    folder = export(target, replace="--replace" in argv, init_git="--no-git" not in argv)
    count = sum(1 for p in folder.rglob("*") if p.is_file() and ".git" not in p.relative_to(folder).parts)
    print(f"Public repository folder ready: {folder} ({count} files, privacy scan clean).")
    print("Next (see docs/PUBLISHING.md): review it, then")
    print(f'  gh repo create {AUTHOR[0]}/timelapse-video-processing --private --source "{folder}" --remote origin --push')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
