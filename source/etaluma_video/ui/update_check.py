"""Help → Check for updates (0.8): compare this version with the latest release on GitHub.

Only on request: the app never checks by itself and never shows anything at start. One HTTPS
request asks the GitHub API for the project's latest release (drafts and pre-releases are never
"latest"); nothing is sent besides that request, and nothing is downloaded or installed. The
answer says whether a newer version exists and offers its release page in the browser.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import APP_NAME, REPOSITORY_URL, VERSION

API = "https://api.github.com/repos/{slug}/releases/latest"


@dataclass
class UpdateResult:
    status: str  # "newer" | "current" | "none" | "error"
    latest: str
    url: str
    message: str


def repository_slug(url: str = REPOSITORY_URL) -> str:
    match = re.match(r"https://github\.com/([^/]+/[^/#?]+)", url.strip())
    if not match:
        raise ValueError(f"Not a GitHub repository address: {url}")
    slug = match.group(1)
    return slug[:-4] if slug.endswith(".git") else slug


def parse_version(text) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$", str(text or ""))
    if not match:
        return None
    major, minor, patch = (int(part or 0) for part in match.groups())
    return major, minor, patch


def check_latest(timeout: float = 10.0, opener=None) -> UpdateResult:
    releases = f"{REPOSITORY_URL}/releases"
    request = urllib.request.Request(API.format(slug=repository_slug()), headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_NAME.replace(' ', '-')}/{VERSION}",
    })
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return UpdateResult("none", "", releases, "No release was found on the project page yet "
                                                      "(the project may not be public yet).")
        if exc.code in (403, 429):
            return UpdateResult("error", "", releases, "GitHub refused the request for now (too many checks "
                                                        "from this network). Try again later.")
        return UpdateResult("error", "", releases, f"GitHub answered with error {exc.code}. Try again later.")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return UpdateResult("error", "", releases, "GitHub could not be reached. Check the internet connection "
                                                    "and try again.")
    tag = str((data or {}).get("tag_name") or "")
    url = str((data or {}).get("html_url") or releases)
    latest, current = parse_version(tag), parse_version(VERSION)
    if latest is None or current is None:
        return UpdateResult("error", tag, url, f"The latest release has an unexpected version name ({tag or 'none'}).")
    name = ".".join(str(n) for n in latest)
    if latest > current:
        return UpdateResult("newer", name, url, f"Version {name} is available; you have {VERSION}.")
    return UpdateResult("current", name, url, f"You have the latest version ({VERSION}).")
