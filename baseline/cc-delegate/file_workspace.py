"""Host-side snapshots and review of a plain directory."""
from contextlib import contextmanager
import ctypes
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata


class WorkspaceError(ValueError):
    pass


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
TEXT_LIMIT = 1024 * 1024
CHUNK = 1024 * 1024


def _limit(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        count = int(value)
        if count > 0:
            return count
    except ValueError:
        pass
    raise WorkspaceError(f"{name} must be a positive integer")


@contextmanager
def _directory(name, parent=None):
    fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_aliases(paths):
    # The project, work copy and delivery can live on different filesystems.
    seen = {}
    for path in paths:
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError(f"workspace path cannot be journaled as UTF-8: {path!r}") from None
        normalized = unicodedata.normalize("NFD", path).casefold()
        old = seen.setdefault(normalized, path)
        if old != path:
            raise WorkspaceError(f"workspace names differ only by case or normalization: {old!r}, {path!r}")


def _scan(root, sensitive):
    max_count = _limit("CC_DELEGATE_FILES_MAX_COUNT", 10_000)
    max_bytes = _limit("CC_DELEGATE_FILES_MAX_BYTES", 1024 ** 3)
    files, directories = {}, set()
    total = 0

    def walk(fd, prefix):
        nonlocal total
        with os.scandir(fd) as entries:
            for entry in entries:
                key = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(info.st_mode)
                if sensitive(key, directory=is_dir) or entry.name.lower() == ".git":
                    raise WorkspaceError(f"refused workspace path: {key!r}")
                if is_dir:
                    directories.add(key)
                    if len(directories) > max_count:
                        raise WorkspaceError(f"workspace directory count exceeds {max_count}")
                    # Bound the descriptor stack even on a tree of empty directories.
                    if key.count("/") >= 128:
                        raise WorkspaceError(f"workspace directory nesting exceeds 128: {key!r}")
                    with _directory(entry.name, fd) as child:
                        walk(child, key + "/")
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1:
                        raise WorkspaceError(f"workspace hard link refused: {key!r}")
                    files[key] = info
                    total += info.st_size
                    if len(files) > max_count:
                        raise WorkspaceError(f"workspace file count exceeds {max_count}")
                    if total > max_bytes:
                        raise WorkspaceError(f"workspace total size exceeds {max_bytes} bytes")
                else:
                    raise WorkspaceError(f"workspace symlink or special file refused: {key!r}")

    walk(root, "")
    _check_aliases(files.keys() | directories)
    return files, directories


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink,
            info.st_ctime_ns, info.st_mtime_ns)


@contextmanager
def _source(root, key, expected):
    # Pin ancestors: a concurrent edit can replace a directory after preflight.
    parts = key.split("/")
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            fd = os.open(part, DIR_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as source:
        if _identity(os.fstat(fd)) != _identity(expected):
            raise WorkspaceError(f"workspace file changed during snapshot: {key!r}")
        yield source
        if _identity(os.fstat(fd)) != _identity(expected):
            raise WorkspaceError(f"workspace file changed during snapshot: {key!r}")


_fclonefileat = getattr(ctypes.CDLL(None), "fclonefileat", None) if sys.platform == "darwin" else None


def _copy(source, destination, size, mode):
    if _fclonefileat is not None:
        # clonefile works on APFS. Keep the source descriptor pinned even when
        # the destination is on a different volume and a byte copy is needed.
        with _directory(destination.parent) as parent:
            cloned = _fclonefileat(source.fileno(), parent, os.fsencode(destination.name), 0) == 0
        if cloned:
            destination.chmod(mode)
            return
    with destination.open("xb") as out:
        remaining = size
        while remaining:
            block = source.read(min(remaining, CHUNK))
            if not block:
                raise WorkspaceError("workspace file shrank during snapshot")
            out.write(block)
            remaining -= len(block)
        if source.read(1):
            raise WorkspaceError("workspace file grew during snapshot")
        os.fchmod(out.fileno(), mode)


def _digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _manifest_file(path, st):
    return dict(type="file", mode=stat.S_IMODE(st.st_mode), size=st.st_size, sha256=_digest(path))


def _json(path, value):
    # Protected run metadata becomes visible only after publication.
    tmp = path.with_suffix(".tmp")
    with tmp.open("x", encoding="ascii") as f:
        json.dump(value, f, ensure_ascii=True, sort_keys=True)
    os.replace(tmp, path)


def prepare(project, d, sensitive, *, dry_run=False):
    """Preflight the complete tree before creating either copy."""
    project, d = Path(project), Path(d)
    if sensitive(project, directory=True):
        raise WorkspaceError("refused_sensitive_path")
    if project == d or d.is_relative_to(project) or project.is_relative_to(d):
        raise WorkspaceError("workspace and project directories overlap")
    base, work = d / "base", d / "work"
    with _directory(project) as root:
        files, directories = _scan(root, sensitive)
        if dry_run:
            return work
        for tree in (base, work):
            tree.mkdir(mode=0o700)
            for key in sorted(directories):
                (tree / key).mkdir(mode=0o700)
        baseline = {}
        for key, st in sorted(files.items()):
            original = base / key
            with _source(root, key, st) as source:
                _copy(source, original, st.st_size, 0o600)
            baseline[key] = _manifest_file(original, st)
            with original.open("rb") as source:
                _copy(source, work / key, st.st_size, baseline[key]["mode"])
    _json(d / "baseline.json", baseline)
    _json(d / "directories.json", sorted(directories))
    return work


def _lines(path, info):
    if info is None:
        return []
    if info["size"] > TEXT_LIMIT:
        return None
    raw = path.read_bytes()
    if re.search(rb"[\x00-\x08\x0b\x0e-\x1f\x7f]", raw):
        return None
    try:
        return raw.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _review(key, before, after, base, delivery):
    name = json.dumps(key, ensure_ascii=False)
    rows = [f"    {name}\n"]
    for label, info in (("before", before), ("after", after)):
        if info is not None:
            rows.append(f"    {label}: {info['size']} bytes, mode {info['mode']:04o}, sha256 {info['sha256']}\n")
    old = _lines(base / key, before)
    new = _lines(delivery / key, after)
    if old is not None and new is not None:
        diff = difflib.unified_diff(old, new, fromfile="a/" + name if before else "/dev/null",
                                    tofile="b/" + name if after else "/dev/null")
        for line in diff:
            rows.append("    " + line)
            if not line.endswith("\n"):
                rows.append("\n    \\ No newline at end of file\n")
    return "".join(rows)


def compare(d, sensitive, *, report_prefix=""):
    """Freeze the reviewed output outside the disposable worker tree."""
    base, work, delivery = d / "base", d / "work", d / "delivery"
    baseline = json.loads((d / "baseline.json").read_text(encoding="ascii"))
    base_dirs = set(json.loads((d / "directories.json").read_text(encoding="ascii")))
    changes, grouped = {}, dict(added=[], deleted=[], modified=[])
    diff_path = d / "file-diff.md"
    with _directory(work) as root, diff_path.open("w", encoding="utf-8", errors="backslashreplace") as report:
        files, directories = _scan(root, sensitive)
        _check_aliases(baseline.keys() | base_dirs | files.keys() | directories)
        if directories.intersection(baseline) or base_dirs.intersection(files):
            raise WorkspaceError("workspace file/directory replacement is unsupported")
        report.write(report_prefix)
        report.write("\n\n## Workspace diff\n\n")
        delivery.mkdir(mode=0o700)
        for key in sorted(baseline.keys() | files.keys()):
            before, after = baseline.get(key), None
            if key in files:
                st = files[key]
                output = delivery / key
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with _source(root, key, st) as source:
                    _copy(source, output, st.st_size, 0o600)
                after = _manifest_file(output, st)
                if before == after:
                    output.unlink()
                    continue
            action = "added" if before is None else "deleted" if after is None else "modified"
            grouped[action].append(key)
            changes[key] = dict(before=before, after=after)
            report.write(_review(key, before, after, base, delivery) + "\n")
    _json(d / "changes.json", changes)
    return grouped, diff_path
