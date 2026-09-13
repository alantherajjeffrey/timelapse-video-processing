"""Fail when files that would be published contain machine paths, user names or addresses.

    python tools/scrub_check.py [folder]          # every file under folder (default: the app folder)
    python tools/scrub_check.py --git [folder]    # only the files git tracks there

Checked: Windows user profile paths (C:\\Users\\<name>), other drive-letter paths into data folders,
e-mail addresses, and any extra words listed in the ETALUMA_SCRUB_WORDS environment variable
(comma-separated, for example a Windows account name). The third-party licence notices keep their
authors' addresses and are skipped for e-mail addresses only. Binary files are skipped.
Exit code 0 when clean, 1 with a list of findings otherwise.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".build"}
SKIP_SUFFIXES = {".exe", ".dll", ".pyd", ".png", ".jpg", ".jpeg", ".ico", ".tif", ".tiff", ".mp4", ".avi", ".npz", ".zip"}
EMAIL_ALLOWED = {"THIRD_PARTY_NOTICES.txt"}
#: The maintainer's public GitHub no-reply address is fine in published files.
EMAIL_ALLOW_PATTERNS = (re.compile(r"@users\.noreply\.github\.com$", re.I), re.compile(r"@(example|anthropic)\.com$", re.I))

PATTERNS = {
    "user profile path": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!<|%|\{|Public\b|Default\b)[^\\/\s\"'<>]+", re.I),
    "data drive path": re.compile(r"\b[D-Z]:[\\/]+(?!Program|Windows)[A-Za-z0-9 _.-]+[\\/]", re.I),
    "e-mail address": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}


def files_to_check(folder: Path, tracked_only: bool) -> list[Path]:
    if tracked_only:
        out = subprocess.run(["git", "-C", str(folder), "ls-files", "-z", "."], capture_output=True, check=True)
        return [folder / p for p in out.stdout.decode("utf-8").split("\0") if p]
    result = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        result.extend(Path(dirpath) / f for f in filenames)
    return result


def scan(folder: Path, tracked_only: bool = False, extra_words: list[str] | None = None) -> list[str]:
    words = [w for w in (extra_words or []) if w]
    findings: list[str] = []
    for path in files_to_check(folder, tracked_only):
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(folder)
        for lineno, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                for m in pattern.finditer(line):
                    if label == "e-mail address" and (path.name in EMAIL_ALLOWED
                                                      or any(p.search(m.group(0)) for p in EMAIL_ALLOW_PATTERNS)):
                        continue
                    findings.append(f"{rel}:{lineno}: {label}: {m.group(0)[:80]}")
            for word in words:
                if word.casefold() in line.casefold():
                    findings.append(f"{rel}:{lineno}: listed word: {word}")
    return findings


def main(argv: list[str]) -> int:
    tracked = "--git" in argv
    args = [a for a in argv if not a.startswith("--")]
    folder = Path(args[0]).resolve() if args else APP_ROOT
    words = [w.strip() for w in os.environ.get("ETALUMA_SCRUB_WORDS", "").split(",")]
    findings = scan(folder, tracked, words)
    for line in findings:
        print(line)
    print(f"{len(findings)} finding(s) in {folder}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
