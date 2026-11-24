"""Host-side delivery of reviewed file workspace changes."""

import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from contextlib import closing, contextmanager


class TransactionError(ValueError):
    pass


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
_INVALID = object()
JOURNAL_NAME = "file-transaction.sqlite3"


@contextmanager
def journal(directory):
    try:
        with closing(sqlite3.connect(directory / JOURNAL_NAME)) as db:
            yield db
    except sqlite3.Error as error:
        raise TransactionError(f"file transaction journal: {error}") from error


def phase(directory, default):
    if not (directory / JOURNAL_NAME).exists():
        return default
    with journal(directory) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='state'").fetchone():
            row = db.execute("SELECT phase FROM state").fetchone()
            if row:
                return row[0]
    return default


@contextmanager
def parent_fd(root, rel):
    fd = os.dup(root)
    try:
        for component in Path(rel).parts[:-1]:
            child = os.open(component, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, Path(rel).name
    finally:
        os.close(fd)


def digest(stream, sink=None):
    h = hashlib.sha256()
    while chunk := stream.read(256 * 1024):
        h.update(chunk)
        if sink:
            sink.write(chunk)
    return h.hexdigest()


def current_file(parent, name, candidates):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return _INVALID
        possible = [item for item in candidates if item and item["size"] == info.st_size
                    and item["mode"] == stat.S_IMODE(info.st_mode)]
        if possible:
            with os.fdopen(os.open(name, _FILE, dir_fd=parent), "rb") as f:
                opened = os.fstat(f.fileno())
                if not os.path.samestat(info, opened) or info.st_ctime_ns != opened.st_ctime_ns:
                    return _INVALID
                value = digest(f)
                if os.fstat(f.fileno()).st_ctime_ns != opened.st_ctime_ns:
                    return _INVALID
            for item in possible:
                if item["sha256"] == value:
                    return item
    except FileNotFoundError:
        return None
    return _INVALID


def inspect(root, rel, candidates):
    """Return a matching manifest entry, absence, or an incompatible object."""
    try:
        with parent_fd(root, rel) as (parent, name):
            return current_file(parent, name, candidates)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno not in (errno.ENOTDIR, errno.ELOOP):
            raise
    return _INVALID


def write_file(source, parent, name, tmp, entry, original, rel):
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as out:
            if digest(source, out) != entry["sha256"]:
                raise TransactionError(f"delivery content changed: {name!r}")
            out.flush()
            os.fchmod(out.fileno(), entry["mode"])
            os.fsync(out.fileno())
        if current_file(parent, name, (original,)) != original:
            require_no_conflicts([rel])
        os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(tmp, dir_fd=parent)
        except FileNotFoundError:
            pass


def backup(root, directory, changes):
    for rel, change in changes.items():
        original = change["before"]
        if original is None:
            continue
        dst = directory / "backup" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with parent_fd(root, rel) as (parent, name):
            with os.fdopen(os.open(name, _FILE, dir_fd=parent), "rb") as src:
                with dst.open("wb") as out:
                    if digest(src, out) != original["sha256"]:
                        raise TransactionError(f"backup conflict: {rel!r}")
                    out.flush()
                    os.fsync(out.fileno())


def ensure_parents(db, root, rel):
    parts = ()
    parent = os.dup(root)
    try:
        for name in Path(rel).parts[:-1]:
            parts += (name,)
            try:
                child = os.open(name, _DIR, dir_fd=parent)
            except FileNotFoundError:
                rel_dir = str(Path(*parts))
                with db:
                    db.execute("INSERT OR IGNORE INTO dirs(path) VALUES (?)", (rel_dir,))
                try:
                    os.mkdir(name, dir_fd=parent)
                except FileExistsError:
                    # A concurrent creator retains ownership of its new directory.
                    with db:
                        db.execute("DELETE FROM dirs WHERE path=?", (rel_dir,))
                child = os.open(name, _DIR, dir_fd=parent)
            info = os.fstat(child)
            with db:
                db.execute("UPDATE dirs SET device=?, inode=? WHERE path=? AND inode IS NULL",
                           (info.st_dev, info.st_ino, str(Path(*parts))))
            os.close(parent)
            parent = child
    finally:
        os.close(parent)


def remove_empty_parents(db, root):
    for rel, device, inode in db.execute("SELECT * FROM dirs ORDER BY length(path) DESC"):
        try:
            with parent_fd(root, rel) as (parent, name):
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (info.st_dev, info.st_ino) == (device, inode):
                    os.rmdir(name, dir_fd=parent)
        except OSError as error:
            # A later user file can keep this directory alive.
            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY, errno.EEXIST):
                raise


def require_no_conflicts(paths):
    if paths:
        raise TransactionError("conflicts=" + json.dumps(paths, ensure_ascii=True))


def operate(db, root, directory, changes, action):
    applying = action == "apply"
    initial = "pending_apply" if applying else "applied"
    active = "applying" if applying else "rolling_back"
    final = "applied" if applying else "rolled_back"
    before, after = ("before", "after") if applying else ("after", "before")
    payload = directory / ("delivery" if applying else "backup")
    row = db.execute("SELECT phase FROM state").fetchone()
    current = row[0] if row else "pending_apply"
    if current == final:
        return final
    if current not in (initial, active):
        raise TransactionError(f"cannot {action} a {current} workspace")

    entries = {rel: (done, tmp) for rel, done, tmp in db.execute(
        "SELECT path, done, tmp FROM progress WHERE action=?", (action,))}
    conflicts = []
    for rel, change in changes.items():
        old, new = change[before], change[after]
        actual = inspect(root, rel, (old, new))
        started = rel in entries
        done = started and entries[rel][0]
        if not ((actual == old and not done) or (started and actual == new)):
            conflicts.append(rel)
    require_no_conflicts(conflicts)

    # Validate the entire payload before the first edit, including on recovery.
    for rel, change in changes.items():
        if change[after] and not entries.get(rel, (False,))[0]:
            with (payload / rel).open("rb") as f:
                if digest(f) != change[after]["sha256"]:
                    raise TransactionError(f"delivery content changed: {rel!r}")

    if current != active:
        if applying:
            backup(root, directory, changes)
        with db:
            db.execute("DELETE FROM state")
            db.execute("INSERT INTO state VALUES (?)", (active,))

    for rel, change in changes.items():
        if entries.get(rel, (False,))[0]:
            continue
        old, new = change[before], change[after]
        started = rel in entries
        tmp = entries[rel][1] if started else ".cc-delegate-" + secrets.token_hex(16)
        actual = inspect(root, rel, (old, new))
        if not (started and actual == new):
            require_no_conflicts([rel] if actual != old else [])
            if new is not None:
                ensure_parents(db, root, rel)
            with parent_fd(root, rel) as (parent, name):
                if started:
                    # Recover a partially written private temporary from a killed writer.
                    try:
                        os.unlink(tmp, dir_fd=parent)
                    except FileNotFoundError:
                        pass
                with db:
                    db.execute("INSERT OR IGNORE INTO progress VALUES (?, ?, 0, ?)",
                               (action, rel, tmp))
                if new is None:
                    if current_file(parent, name, (old,)) != old:
                        require_no_conflicts([rel])
                    os.unlink(name, dir_fd=parent)
                else:
                    with (payload / rel).open("rb") as source:
                        write_file(source, parent, name, tmp, new, old, rel)
                os.fsync(parent)
        with db:
            db.execute("UPDATE progress SET done=1 WHERE action=? AND path=?", (action, rel))

    if not applying:
        remove_empty_parents(db, root)
    with db:
        db.execute("UPDATE state SET phase=?", (final,))
    return final


def transact(directory, meta, action):
    """Caller holds the project and run locks for the full transaction."""
    if action not in ("apply", "rollback"):
        raise TransactionError("invalid file operation")
    changes = json.loads((directory / "changes.json").read_text())
    for rel in changes:
        path = Path(rel)
        if not path.parts or path.is_absolute() or ".." in path.parts or str(path) != rel:
            raise TransactionError("invalid change path")
    root = os.open(meta["project_dir"], _DIR)
    try:
        attrs = os.fstat(root)
        if meta.get("project_identity") != [attrs.st_dev, attrs.st_ino]:
            raise TransactionError("project directory replaced")
        with journal(directory) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state (phase TEXT);
                CREATE TABLE IF NOT EXISTS dirs (path TEXT PRIMARY KEY, device INTEGER, inode INTEGER);
                CREATE TABLE IF NOT EXISTS progress (
                    action TEXT, path TEXT, done INTEGER, tmp TEXT, PRIMARY KEY (action,path));
            """)
            return operate(db, root, directory, changes, action)
    finally:
        os.close(root)
