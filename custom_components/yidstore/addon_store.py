"""Local add-on store served to the Supervisor over git's smart HTTP protocol.

Background
----------
On Home Assistant OS the Core process (where this integration runs) cannot
write to the folder the Supervisor scans for *local* add-ons. The only way
Core can get an add-on installed by itself is to hand the Supervisor a git
**store repository** that it clones and installs from.

YidStore uses this to ship the **YidStore Connector** add-on inside the
integration: no separate repository is needed. The Supervisor only ever sees
an ``http://homeassistant:8123/api/yidstore/store.git`` URL.

Why smart HTTP (and no git binary)
----------------------------------
The Supervisor clones store repositories with ``--depth 1``. git's "dumb"
HTTP transport refuses shallow clones, so the repository is served with the
smart protocol (``info/refs?service=git-upload-pack`` + ``git-upload-pack``).
The repository is tiny and has a single root commit, so the server simply
answers every fetch with one pack holding all objects; no negotiation or
delta compression is needed. Everything is generated in pure Python.

Layout on disk (under a writable /config path)::

    <root>/src/                 worktree: repository.json + one dir per add-on
    <root>/repo.git/HEAD_SHA    commit id of the published tree
    <root>/repo.git/store.pack  packfile with every object of that commit

Publishing is idempotent: unchanged content yields the same commit id, so
re-publishing does not make the Supervisor see a change.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import shutil
import struct
import zipfile
import zlib
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Fixed identity/timestamp keeps commit ids reproducible.
_IDENT = "YidStore <store@yidstore.local>"
_WHEN = "1700000000 +0000"
_BRANCH = "refs/heads/main"
_OBJ_TYPES = {b"commit": 1, b"tree": 2, b"blob": 3}


# ---------------------------------------------------------------------------
# Building the repository
# ---------------------------------------------------------------------------

def _object(objects: dict[str, tuple[bytes, bytes]], obj_type: bytes, content: bytes) -> str:
    """Record a git object and return its SHA-1."""
    header = obj_type + b" " + str(len(content)).encode() + b"\x00"
    sha = hashlib.sha1(header + content).hexdigest()
    objects[sha] = (obj_type, content)
    return sha


def _tree_sort_key(entry: tuple[str, str, str]) -> bytes:
    """Git orders tree entries by name, treating directories as ``name/``."""
    mode, name, _sha = entry
    return name.encode() + (b"/" if mode == "40000" else b"")


def _write_tree(objects: dict, node: dict) -> str:
    entries: list[tuple[str, str, str]] = []
    for name, value in node.items():
        if isinstance(value, dict):
            entries.append(("40000", name, _write_tree(objects, value)))
        else:
            mode = "100755" if name.endswith(".sh") else "100644"
            entries.append((mode, name, _object(objects, b"blob", value)))
    buf = bytearray()
    for mode, name, sha in sorted(entries, key=_tree_sort_key):
        buf += mode.encode() + b" " + name.encode() + b"\x00" + bytes.fromhex(sha)
    return _object(objects, b"tree", bytes(buf))


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
    files: dict[str, bytes] = {}
    for path in src_dir.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(src_dir).as_posix()] = path.read_bytes()
    return files


def _pack(objects: dict[str, tuple[bytes, bytes]]) -> bytes:
    """Build a version 2 packfile with every object stored whole."""
    out = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
    for obj_type, content in objects.values():
        size = len(content)
        byte = (_OBJ_TYPES[obj_type] << 4) | (size & 0x0F)
        size >>= 4
        while size:
            out.append(byte | 0x80)
            byte = size & 0x7F
            size >>= 7
        out.append(byte)
        out += zlib.compress(content)
    out += hashlib.sha1(out).digest()
    return bytes(out)


def publish_from_dir(root: Path) -> str | None:
    """(Re)generate the served repository from ``root/src``. Returns the commit id."""
    src_dir = root / "src"
    git_dir = root / "repo.git"
    if not src_dir.is_dir():
        return None
    files = _collect_files(src_dir)
    if not files:
        return None

    objects: dict[str, tuple[bytes, bytes]] = {}
    tree_sha = _write_tree(objects, _nest(files))
    commit_body = (
        f"tree {tree_sha}\n"
        f"author {_IDENT} {_WHEN}\n"
        f"committer {_IDENT} {_WHEN}\n\n"
        "YidStore add-on store\n"
    ).encode()
    commit_sha = _object(objects, b"commit", commit_body)

    git_dir.mkdir(parents=True, exist_ok=True)
    head_file = git_dir / "HEAD_SHA"
    if head_file.is_file() and head_file.read_bytes().decode().strip() == commit_sha:
        return commit_sha
    pack_tmp = git_dir / "store.pack.tmp"
    pack_tmp.write_bytes(_pack(objects))
    pack_tmp.replace(git_dir / "store.pack")
    head_file.write_bytes(commit_sha.encode())
    _LOGGER.info("Published YidStore add-on store (commit %s)", commit_sha[:12])
    return commit_sha


def ensure_repository_json(root: Path, name: str = "YidStore") -> None:
    """Write the store's ``repository.json`` if missing."""
    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    repo_json = src_dir / "repository.json"
    if not repo_json.exists():
        repo_json.write_bytes(json.dumps({
            "name": name,
            "url": "https://onoffautomations.com",
            "maintainer": "OnOff Automations",
        }, indent=2).encode())


def sync_addon_dir(root: Path, slug: str, source: Path) -> None:
    """Copy an add-on folder (e.g. the bundled connector) into the store."""
    dest = root / "src" / slug
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__"))


def add_addon_from_zip(root: Path, slug: str, zip_bytes: bytes) -> dict:
    """Extract a Gitea archive into the store under ``src/<slug>/``.

    Gitea zipballs wrap everything in a single top-level ``<repo>/`` folder;
    that wrapper is stripped so ``config.yaml`` lands at the root of ``<slug>/``.
    """
    src_dir = root / "src" / slug
    if src_dir.exists():
        shutil.rmtree(src_dir)
    src_dir.mkdir(parents=True, exist_ok=True)

    config_found = False
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        top = None
        first_parts = [n.split("/", 1) for n in names]
        if first_parts and all(len(p) == 2 for p in first_parts):
            tops = {p[0] for p in first_parts}
            if len(tops) == 1:
                top = next(iter(tops))
        for name in names:
            rel = name[len(top) + 1:] if top else name
            if not rel or ".." in rel.split("/"):
                continue
            dest = src_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(name))
            if rel.lower() in ("config.yaml", "config.yml", "config.json"):
                config_found = True

    return {"slug": slug, "config_found": config_found, "src_path": str(src_dir)}


# ---------------------------------------------------------------------------
# Smart HTTP protocol (git-upload-pack, protocol v0, stateless)
# ---------------------------------------------------------------------------

def _pkt(data: bytes) -> bytes:
    return f"{len(data) + 4:04x}".encode() + data


_FLUSH = b"0000"


def _head(git_dir: Path) -> str | None:
    head_file = git_dir / "HEAD_SHA"
    if not head_file.is_file():
        return None
    sha = head_file.read_bytes().decode().strip()
    return sha or None


def advertise_refs(git_dir: Path) -> bytes | None:
    """Body for ``GET info/refs?service=git-upload-pack``."""
    sha = _head(git_dir)
    if not sha:
        return None
    caps = f"shallow no-progress symref=HEAD:{_BRANCH} agent=yidstore/1"
    return (
        _pkt(b"# service=git-upload-pack\n")
        + _FLUSH
        + _pkt(f"{sha} HEAD\x00{caps}\n".encode())
        + _pkt(f"{sha} {_BRANCH}\n".encode())
        + _FLUSH
    )


def _read_pkts(body: bytes) -> list[str | None]:
    """Parse pkt-lines; ``None`` stands for a flush packet."""
    out: list[str | None] = []
    i = 0
    while i + 4 <= len(body):
        try:
            length = int(body[i:i + 4], 16)
        except ValueError:
            break
        if length == 0:
            out.append(None)
            i += 4
            continue
        if length < 4:
            i += 4
            continue
        out.append(body[i + 4:i + length].decode("utf-8", "replace").rstrip("\n"))
        i += length
    return out


def upload_pack(git_dir: Path, body: bytes, content_encoding: str = "") -> bytes:
    """Body for ``POST git-upload-pack``.

    The repository has a single root commit, so a depth-limited fetch needs
    no shallow boundary, and every want is answered with the full pack.
    """
    if "gzip" in (content_encoding or "").lower():
        body = gzip.decompress(body)
    lines = _read_pkts(body)
    wants = [ln.split()[1] for ln in lines if ln and ln.startswith("want ")]
    deepen = any(ln and ln.startswith(("deepen", "shallow ")) for ln in lines)
    done = any(ln == "done" for ln in lines)

    out = bytearray()
    if deepen:
        # Shallow-update section, sent on every stateless round. Like
        # git upload-pack, the wanted tip is reported as the shallow
        # boundary (even though it is a root commit).
        for sha in dict.fromkeys(wants):
            out += _pkt(f"shallow {sha}\n".encode())
        out += _FLUSH
    if not wants or not done:
        # Stateless negotiation round: the client follows up with "done".
        return bytes(out)
    out += _pkt(b"NAK\n")
    pack = git_dir / "store.pack"
    out += pack.read_bytes() if pack.is_file() else b""
    return bytes(out)
