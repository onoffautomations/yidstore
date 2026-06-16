"""Local add-on store served to the Supervisor over git's "dumb HTTP".

Background
----------
On Home Assistant OS the Core process (where this integration runs) cannot
write to the folder the Supervisor scans for *local* add-ons, so dropping
files there never makes an add-on appear.  The only way Core can get an
add-on installed on HA OS is to hand the Supervisor a git **store
repository** that it clones and installs from.

To honour the rule that the upstream Gitea URL (git.onoffapi.com) must never
be exposed, YidStore publishes its *own* add-on store: it fetches the add-on
sources from Gitea server-side and republishes them locally.  The Supervisor
only ever sees a ``http://homeassistant:8123/...`` URL.

Why "dumb HTTP"
---------------
git's dumb HTTP transport needs no git binary and no third-party package: the
server just exposes a directory of *loose* git objects plus a couple of ref
files as static content, and the client walks the object graph over plain
GETs.  We therefore build the repository by writing loose objects ourselves.

Layout on disk (all under a writable /config path)::

    <root>/src/                 plain worktree-style cache (one dir per add-on)
    <root>/src/repository.json  store metadata
    <root>/repo.git/            generated loose-object repo served over HTTP

``publish_from_dir`` regenerates ``repo.git`` from ``src`` and is idempotent:
unchanged content yields the same commit hash, so re-publishing is cheap and
does not churn the Supervisor.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
import zlib
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Fixed identity/timestamp keeps commit hashes reproducible: republishing the
# same sources produces the same commit, so the Supervisor sees no change.
_IDENT = "YidStore <store@yidstore.local>"
_WHEN = "1700000000 +0000"


def _store_object(objects_dir: Path, obj_type: bytes, content: bytes) -> str:
    """Write a single loose git object and return its 40-char SHA-1."""
    header = obj_type + b" " + str(len(content)).encode() + b"\x00"
    raw = header + content
    sha = hashlib.sha1(raw).hexdigest()
    dest = objects_dir / sha[:2] / sha[2:]
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zlib.compress(raw))
    return sha


def _tree_sort_key(entry: tuple[str, str, str]) -> bytes:
    """Git orders tree entries by name, treating directories as ``name/``."""
    mode, name, _sha = entry
    suffix = b"/" if mode == "40000" else b""
    return name.encode() + suffix


def _write_tree(objects_dir: Path, node: dict) -> str:
    """Recursively write a tree object for ``node`` (nested dict of bytes)."""
    entries: list[tuple[str, str, str]] = []
    for name, value in node.items():
        if isinstance(value, dict):
            entries.append(("40000", name, _write_tree(objects_dir, value)))
        else:
            mode = "100755" if name.endswith(".sh") else "100644"
            entries.append((mode, name, _store_object(objects_dir, b"blob", value)))

    buf = bytearray()
    for mode, name, sha in sorted(entries, key=_tree_sort_key):
        buf += mode.encode() + b" " + name.encode() + b"\x00" + bytes.fromhex(sha)
    return _store_object(objects_dir, b"tree", bytes(buf))


def _nest(files: dict[str, bytes]) -> dict:
    """Turn a flat ``path -> bytes`` map into a nested directory dict."""
    root: dict = {}
    for path, data in files.items():
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):  # a file already claimed this name
                break
        else:
            node[parts[-1]] = data
    return root


def _collect_files(src_dir: Path) -> dict[str, bytes]:
    """Read every file under ``src_dir`` into a ``relpath -> bytes`` map."""
    files: dict[str, bytes] = {}
    for path in src_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(src_dir).as_posix()
            files[rel] = path.read_bytes()
    return files


def publish_from_dir(root: Path) -> str | None:
    """(Re)generate ``root/repo.git`` from ``root/src``. Returns commit SHA."""
    src_dir = root / "src"
    git_dir = root / "repo.git"
    if not src_dir.is_dir():
        return None

    files = _collect_files(src_dir)
    if not files:
        return None

    objects_dir = git_dir / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)

    tree_sha = _write_tree(objects_dir, _nest(files))
    commit_body = (
        f"tree {tree_sha}\n"
        f"author {_IDENT} {_WHEN}\n"
        f"committer {_IDENT} {_WHEN}\n\n"
        "YidStore add-on store\n"
    ).encode()
    commit_sha = _store_object(objects_dir, b"commit", commit_body)

    # Refs + HEAD.
    heads = git_dir / "refs" / "heads"
    heads.mkdir(parents=True, exist_ok=True)
    (heads / "main").write_text(commit_sha + "\n")
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")

    # Dumb-protocol metadata: advertised refs + (empty) pack list.
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "refs").write_text(f"{commit_sha}\trefs/heads/main\n")
    (objects_dir / "info").mkdir(parents=True, exist_ok=True)
    (objects_dir / "info" / "packs").write_text("")

    _LOGGER.info("Published YidStore add-on store at %s (commit %s)", git_dir, commit_sha)
    return commit_sha


def ensure_repository_json(root: Path, name: str = "YidStore Add-ons") -> None:
    """Write the store's ``repository.json`` if missing."""
    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    repo_json = src_dir / "repository.json"
    if not repo_json.exists():
        repo_json.write_text(json.dumps({
            "name": name,
            "url": "https://onoffautomations.com",
            "maintainer": "OnOff Automations",
        }, indent=2))


def add_addon_from_zip(root: Path, slug: str, zip_bytes: bytes) -> dict:
    """Extract a Gitea archive into the store under ``src/<slug>/``.

    Gitea zipballs wrap everything in a single top-level ``<repo>-<ref>/``
    folder; that wrapper is stripped so the add-on's ``config.yaml`` /
    ``Dockerfile`` land at the root of ``<slug>/``.
    """
    src_dir = root / "src" / slug
    if src_dir.exists():
        import shutil
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)

    config_found = False
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        # Detect and strip the common top-level wrapper folder.
        top = None
        first_parts = [n.split("/", 1) for n in names]
        if first_parts and all(len(p) == 2 for p in first_parts):
            tops = {p[0] for p in first_parts}
            if len(tops) == 1:
                top = next(iter(tops))
        for name in names:
            rel = name[len(top) + 1:] if top else name
            if not rel:
                continue
            dest = src_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
            base = rel.rsplit("/", 1)[-1].lower()
            if base in ("config.yaml", "config.yml", "config.json"):
                config_found = True

    return {"slug": slug, "config_found": config_found, "src_path": str(src_dir)}


# Content types for the handful of dumb-HTTP paths the Supervisor requests.
def git_content_type(rel_path: str) -> str:
    if rel_path.endswith("info/refs"):
        return "text/plain; charset=utf-8"
    if rel_path == "HEAD" or rel_path.startswith("refs/"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"
