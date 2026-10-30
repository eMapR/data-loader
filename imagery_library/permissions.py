"""File permissions of the library: read-only for researchers, controlled
updates by maintainers. Applied by every maintainer command (import,
acquire, verify, catalog, add-product) to what it wrote, and checkable by
anyone with `imagery-library permissions ROOT` (`--fix` for maintainers).

Policy (LIBRARY.yaml `permissions:`, all optional; defaults below):

    group: imglib-maint   maintainers' group: owns everything (default: leave group as is)
    dirs: "2775"          maintainers create/rename files; others list + read, never create/rename/delete
    imagery: "0444"       GeoTIFFs and USGS source records once acquired: nobody edits them in place
                          (a maintainer can still replace a damaged one: rename needs only the directory)
    metadata: "0644"      manifest, items, request, lineage, product spec, catalog: rewritten atomically
    admin_dirs: "2770"    admin/, staging/ and each tile's .state/: maintainers only
    admin_files: "0660"   logs, locks, verify records, staged work, DataLoader's run journal and lock

Researchers get read access through "other" (or an ACL the storage admins
add). Files are written by DataLoader with the maintainer's umask and any
default ACL; this module brings them to the policy after each operation.
Changing the mode of a hard-linked file changes it for every name of that
file, so a file with more than one name (during a hard-link migration, the
same file is also in the old project-named archive) is never changed here:
it is counted as `shared` and left exactly as it is. Once the old names
are retired the file has one name and the policy applies to it as usual.
(A file with two names inside the library, e.g. one source record two
tiles share, is skipped the same way; USGS ARD records are per tile.)
"""
from __future__ import annotations

import grp
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from imagery_library.layout import ADMIN_DIR, CATALOG_DIR, RESERVED, STAGING_DIR, LibraryError

_KEYS = {"group", "dirs", "imagery", "metadata", "admin_dirs", "admin_files"}
DEFAULTS = {"dirs": "2775", "imagery": "0444", "metadata": "0644", "admin_dirs": "2770", "admin_files": "0660"}
_IMAGERY_TOP = {"landsat", "sentinel2", "metadata"}  # tile subtrees holding acquired files


@dataclass
class Policy:
    group: Optional[str]
    gid: Optional[int]
    modes: dict

    @classmethod
    def from_library(cls, lib) -> "Policy":
        d = lib.info.get("permissions") or {}
        if not isinstance(d, dict):
            raise LibraryError("LIBRARY.yaml permissions: expected a mapping")
        extra = sorted(set(d) - _KEYS)
        if extra:
            raise LibraryError(f"LIBRARY.yaml permissions: unknown key(s) {', '.join(extra)} "
                               f"(allowed: {', '.join(sorted(_KEYS))})")
        modes = {}
        for k, default in DEFAULTS.items():
            v = str(d.get(k, default))
            try:
                modes[k] = int(v, 8)
            except ValueError:
                raise LibraryError(f"LIBRARY.yaml permissions.{k}: {v!r} is not an octal mode") from None
        if modes["imagery"] & 0o222 & ~0o200:
            raise LibraryError("LIBRARY.yaml permissions.imagery must not be group/other-writable")
        if modes["dirs"] & 0o002 or modes["metadata"] & 0o002:
            raise LibraryError("LIBRARY.yaml permissions: dirs/metadata must not be writable by others")
        group = d.get("group")
        gid = None
        if group is not None:
            try:
                gid = grp.getgrnam(str(group)).gr_gid
            except KeyError:
                raise LibraryError(f"LIBRARY.yaml permissions.group: no group {group!r} on this system") from None
        return cls(group, gid, modes)

    def mode_for(self, lib_root: Path, path: Path, is_dir: bool) -> int:
        rel = path.relative_to(lib_root).parts
        private = bool(rel) and (rel[0] in (ADMIN_DIR, STAGING_DIR) or ".state" in rel)
        if is_dir:
            return self.modes["admin_dirs"] if private else self.modes["dirs"]
        if private or (rel and rel[0] == CATALOG_DIR and rel[-1].startswith(".")):  # catalog build lock
            return self.modes["admin_files"]
        if rel and rel[0] not in RESERVED and any(p in _IMAGERY_TOP for p in rel[3:-1]):
            return self.modes["imagery"]  # <version>/<grid dir>/<tile>/(landsat|metadata)/...
        return self.modes["metadata"]


@dataclass
class Report:
    checked: int = 0
    changed: int = 0
    problems: list = field(default_factory=list)
    acl: int = 0
    shared: int = 0        # files with another name outside the library: left as they are


def _walk(top: Path) -> Iterable[tuple[Path, bool]]:
    if top.is_symlink() or not top.exists():
        return
    yield top, top.is_dir()
    if top.is_dir():
        for dirpath, dirnames, filenames in os.walk(top):
            d = Path(dirpath)
            for n in dirnames:
                p = d / n
                if not p.is_symlink():
                    yield p, True
            for n in filenames:
                p = d / n
                if not p.is_symlink():
                    yield p, False


def _has_acl(path: Path) -> bool:
    try:
        return any(a.startswith("system.posix_acl") for a in os.listxattr(path, follow_symlinks=False))
    except OSError:
        return False


def apply(lib, paths: Optional[list] = None, *, fix: bool = True, policy: Optional[Policy] = None,
          examples: int = 20) -> Report:
    """Bring `paths` (default: the whole library) to the policy, or with
    fix=False only report what differs."""
    policy = policy or Policy.from_library(lib)
    rep = Report()
    root = lib.root
    tops = [Path(p) for p in (paths or [root])]
    for top in tops:
        for p, is_dir in _walk(top):
            rep.checked += 1
            st = p.lstat()
            if not is_dir and st.st_nlink > 1:
                rep.shared += 1
                continue
            want = policy.mode_for(root, p, is_dir)
            have = stat.S_IMODE(st.st_mode)
            wrong_group = policy.gid is not None and st.st_gid != policy.gid
            if _has_acl(p):
                rep.acl += 1
            if have == want and not wrong_group:
                continue
            if not fix:
                if len(rep.problems) < examples:
                    rep.problems.append(f"{p.relative_to(root)}: mode {have:04o}, want {want:04o}"
                                        + (f"; group {st.st_gid}, want {policy.group}" if wrong_group else ""))
                else:
                    rep.problems.append(None)
                continue
            try:
                if wrong_group:
                    os.chown(p, -1, policy.gid)
                os.chmod(p, want)
                rep.changed += 1
            except PermissionError as e:
                rep.problems.append(f"{p.relative_to(root)}: {e.strerror} (only the owner or root can change it)")
                continue
            got = stat.S_IMODE(p.lstat().st_mode)
            if got != want:  # Linux silently drops setgid for a group the caller is not in
                rep.problems.append(f"{p.relative_to(root)}: mode is {got:04o} after setting {want:04o}; the system "
                                    "dropped the setgid bit because you are not in the file's group -- set "
                                    "permissions.group in LIBRARY.yaml to the maintainers' group")
    return rep


def apply_quietly(lib, paths: list) -> None:
    """Used after maintainer operations; a file another maintainer owns is
    reported by `imagery-library permissions`, not raised here."""
    apply(lib, paths, fix=True)
