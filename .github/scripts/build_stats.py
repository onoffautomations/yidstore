"""Build stats.json: stars and downloads for every YidStore item.

Runs in GitHub Actions (see .github/workflows/stats.yml). Reads only public
data, with no token: every public repository on the YidStore store server
(Gitea) and the GitHub repositories listed below. If a GITEA_TOKEN secret
is set, private store repositories that token can see are included too.

Items are keyed by a short hash of "<source>:<owner>/<repo>" (lower-case),
never by name, so the public file doesn't reveal private repository names.
YidStore computes the same hash to look items up (custom_components/yidstore/stats.py).

Downloads = release asset downloads + release source-archive downloads
(Gitea counts the zip/tar.gz of a release tag, which is what YidStore
downloads when it installs a release).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Same store server the integration uses (custom_components/yidstore/_utils.py).
GITEA_URL = os.environ.get("GITEA_URL") or base64.b64decode(
    "aHR0cHM6Ly9naXQub25vZmZhcGkuY29t"
).decode()
GITEA_TOKEN = os.environ.get("GITEA_TOKEN") or ""
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or ""

# GitHub repositories YidStore offers directly.
GITHUB_REPOS = ["onoffautomations/yidstore"]


def item_key(source: str, owner: str, repo: str) -> str:
    return hashlib.sha256(f"{source}:{owner}/{repo}".lower().encode()).hexdigest()[:16]


def get_json(url: str, headers: dict | None = None, retries: int = 3):
    req_headers = {"Accept": "application/json", "User-Agent": "YidStore-Stats"}
    req_headers.update(headers or {})
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "null")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return None
            if attempt == retries - 1:
                raise
        except Exception:
            if attempt == retries - 1:
                raise
        time.sleep(2 * (attempt + 1))
    return None


def gitea_headers() -> dict:
    return {"Authorization": f"token {GITEA_TOKEN}"} if GITEA_TOKEN else {}


def release_downloads(releases: list) -> int:
    total = 0
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        for asset in rel.get("assets") or []:
            total += int(asset.get("download_count") or 0)
        archive = rel.get("archive_download_count") or {}
        total += int(archive.get("zip") or 0) + int(archive.get("tar_gz") or 0)
    return total


def gitea_items() -> dict:
    items: dict = {}
    page = 1
    while True:
        url = f"{GITEA_URL}/api/v1/repos/search?limit=50&page={page}"
        data = get_json(url, gitea_headers())
        repos = (data or {}).get("data") or []
        if not repos:
            break
        for repo in repos:
            owner = (repo.get("owner") or {}).get("login") or ""
            name = repo.get("name") or ""
            if not owner or not name or repo.get("archived"):
                continue
            downloads = 0
            if repo.get("release_counter", 1):
                rel_page = 1
                while True:
                    rels = get_json(
                        f"{GITEA_URL}/api/v1/repos/{urllib.parse.quote(owner)}/"
                        f"{urllib.parse.quote(name)}/releases?limit=50&page={rel_page}",
                        gitea_headers(),
                    ) or []
                    downloads += release_downloads(rels)
                    if len(rels) < 50:
                        break
                    rel_page += 1
            items[item_key("gitea", owner, name)] = {
                "stars": int(repo.get("stars_count") or 0),
                "downloads": downloads,
            }
        page += 1
    return items


def github_items() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    items: dict = {}
    for full in GITHUB_REPOS:
        owner, _, name = full.partition("/")
        info = get_json(f"https://api.github.com/repos/{full}", headers)
        if not isinstance(info, dict):
            continue
        downloads = 0
        page = 1
        while True:
            rels = get_json(
                f"https://api.github.com/repos/{full}/releases?per_page=100&page={page}", headers
            ) or []
            downloads += release_downloads(rels)
            if len(rels) < 100:
                break
            page += 1
        items[item_key("github", owner, name)] = {
            "stars": int(info.get("stargazers_count") or 0),
            "downloads": downloads,
        }
    return items


def main() -> int:
    items = {}
    items.update(gitea_items())
    items.update(github_items())
    if not items:
        print("No items found; keeping the previous stats", file=sys.stderr)
        return 1
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "items": dict(sorted(items.items())),
    }
    with open(sys.argv[1] if len(sys.argv) > 1 else "stats.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=False)
        fh.write("\n")
    print(f"Wrote stats for {len(items)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main())
