"""
db.py - SQLite storage layer.

One file, mediavault.db. Each scan replaces that drive's previous snapshot,
so the data always reflects the last time the drive was scanned.
"""

import os
import re
import shutil
import sqlite3
import urllib.request
from datetime import datetime, timezone

import matching
import tagging

DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediavault.db")

# MEDIAVAULT_DB wins, then whatever Settings last saved, then next to the
# code. Read through a function so a change at runtime takes effect without a
# restart, but cached because every query asks.
_db_path = None


def db_path():
    global _db_path
    if _db_path is None:
        _db_path = os.environ.get("MEDIAVAULT_DB") or _stored_db_path() or DEFAULT_DB_PATH
    return _db_path


def _stored_db_path():
    """Read the saved path without importing config, which imports nothing but
    would still make this module depend on it."""
    try:
        import json
        setting = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(setting, "r", encoding="utf-8") as f:
            return (json.load(f).get("db_path") or "").strip() or None
    except (OSError, ValueError, AttributeError):
        return None


def set_db_path(path):
    """Point at a different database file from here on."""
    global _db_path
    _db_path = path


def resolve_db_path(wanted):
    """
    The file `wanted` would put the database at. A folder gets the current
    file name appended, since that is what the Settings picker hands back.
    """
    current = db_path()
    path = os.path.abspath(wanted)
    if os.path.isdir(path):
        path = os.path.join(path, os.path.basename(current))
    return path


def describe_database(path):
    """
    What another database file holds, so it can be told apart from the current
    one. None if there is no MediaVault database there.

    Read-only: this runs on a file the user may well decide not to use, and
    looking inside must not change it.
    """
    if not os.path.isfile(path):
        return None
    try:
        uri = "file:" + urllib.request.pathname2url(os.path.abspath(path)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if not {"drives", "nodes"} <= tables:
                return None
            return {
                "drives": conn.execute("SELECT COUNT(*) FROM drives").fetchone()[0],
                "titles": conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE depth = 2").fetchone()[0],
                "tags": (conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
                         if "tags" in tables else 0),
                "last_scanned": conn.execute(
                    "SELECT MAX(last_scanned) FROM drives").fetchone()[0],
                "size_bytes": os.path.getsize(path),
            }
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None


JOURNAL_SUFFIXES = ("-wal", "-shm")


def _sideline(path):
    """
    Move a database out of the way and return where it went.

    Renamed rather than deleted: replacing a database whose contents the user
    has not seen needs a way back. The -wal goes with it, or SQLite would
    apply the old one to whatever lands in its place.
    """
    kept = f"{path}.replaced-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, kept)
    except OSError as e:
        raise OSError(
            f"The database already at {path} is in use, so nothing was "
            f"changed: {e}\nClose whatever has it open and try again."
        )
    for suffix in JOURNAL_SUFFIXES:
        if os.path.exists(path + suffix):
            os.replace(path + suffix, kept + suffix)
    return kept


def _restore(kept, path):
    """Undo _sideline, for a failure after it but before the new file landed."""
    os.replace(kept, path)
    for suffix in JOURNAL_SUFFIXES:
        if os.path.exists(kept + suffix):
            os.replace(kept + suffix, path + suffix)


def adopt_database(new_path):
    """
    Read from the database already at new_path from here on.

    Nothing is copied and nothing is deleted: the file currently in use stays
    exactly where it is. Migrations run on the adopted file, since it may have
    been written by an older version.
    """
    if describe_database(new_path) is None:
        raise OSError(f"There is no MediaVault database at {new_path}.")
    set_db_path(os.path.abspath(new_path))
    init_db()
    return db_path()


def move_database(new_path, replace=False):
    """
    Move the database to another folder and read from there afterwards.

    Copies, checks the copy arrived, then removes the original, which is the
    same order moveops uses and for the same reason: a failure half way must
    not be able to lose the file. An occupied destination is refused unless
    replace is set, which sidelines what is there instead of overwriting it.

    Returns {'path': where it now lives, 'replaced': what was moved aside}.

    The WAL is checkpointed first, or recent commits would be left behind in
    a -wal file beside the old path.
    """
    current = db_path()
    new_path = resolve_db_path(new_path)
    if os.path.normcase(new_path) == os.path.normcase(current):
        return {"path": current, "replaced": None}

    folder = os.path.dirname(new_path)
    if folder and not os.path.isdir(folder):
        raise OSError(f"No such folder: {folder}")
    if os.path.exists(new_path) and not replace:
        raise OSError(f"There is already a file at {new_path}. Move or rename it first.")

    replaced = None
    if os.path.exists(current):
        conn = sqlite3.connect(current)
        try:
            # The result has to be checked. A blocked checkpoint returns a
            # busy flag rather than raising, and moving only the .db while
            # committed rows are still in the -wal loses them silently.
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
        if row is not None and row[0] != 0:
            raise OSError(
                "Some writes are still pending and could not be flushed, so "
                "moving now would lose them. Close any other copy of the "
                "dashboard and try again."
            )
        wal = current + "-wal"
        if os.path.exists(wal) and os.path.getsize(wal) > 0:
            raise OSError(
                "There are still unflushed writes beside the database, so "
                "nothing was moved. Close any other copy of the dashboard "
                "and try again."
            )

        # Last, so a check above failing leaves the destination untouched.
        if replace and os.path.exists(new_path):
            replaced = _sideline(new_path)

        def undo():
            """Put the destination back as it was found."""
            try:
                os.remove(new_path)
            except OSError:
                pass
            if replaced:
                _restore(replaced, new_path)

        try:
            # Not os.replace: a rename cannot cross drives on Windows, and
            # moving the database onto another drive is the point of this.
            shutil.copy2(current, new_path)
            if os.path.getsize(new_path) != os.path.getsize(current):
                raise OSError("The copy came up short, so nothing was moved.")
            os.remove(current)
        except OSError as e:
            # Either the copy failed, or it worked and something still holds
            # the original open, usually a scan or a second dashboard. Both
            # end the same way: leave the destination exactly as it was found
            # rather than half moved.
            undo()
            raise OSError(
                f"Nothing was moved: {e}\nWait for any scan to finish, close "
                f"any other copy of the dashboard, and try again."
            )

        # Rebuilt on demand, so they are cleared rather than carried across to
        # sit beside a database that is no longer there.
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(current + suffix)
            except OSError:
                pass

    set_db_path(new_path)
    return {"path": new_path, "replaced": replaced}


# Where the database was when this process started. A snapshot, so anything
# that has to survive Settings pointing the app elsewhere calls db_path().
DB_PATH = db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    drive_id        TEXT PRIMARY KEY,   -- stable id (see driveid.py), not a drive letter
    label           TEXT,               -- human name, e.g. "Seagate 4TB Blue"
    total_bytes     INTEGER,
    used_bytes      INTEGER,
    free_bytes      INTEGER,
    last_seen_path  TEXT,               -- last known mount path/letter, just for reference
    last_scanned    TEXT                -- ISO timestamp
);

-- Columns added after the first release are handled by _migrate_schema()
-- below, since SQLite cannot ADD COLUMN IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_id    TEXT NOT NULL,
    parent_id   INTEGER,               -- NULL = this is a library root (e.g. "Videos_SSD1")
    name        TEXT NOT NULL,
    rel_path    TEXT NOT NULL,         -- path relative to drive root
    is_dir      INTEGER NOT NULL,      -- 1 = folder, 0 = file
    size_bytes  INTEGER NOT NULL,      -- recursive total for folders, file size for files
    depth       INTEGER NOT NULL,      -- 0 = library/redundancy root folder itself
    root_type   TEXT NOT NULL DEFAULT 'library',  -- 'library' or 'redundancy'
    FOREIGN KEY (drive_id) REFERENCES drives(drive_id),
    FOREIGN KEY (parent_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_drive ON nodes(drive_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
-- Serves the shallow (depth <= 2) page render and the depth-2 duplicate scan.
CREATE INDEX IF NOT EXISTS idx_nodes_drive_depth ON nodes(drive_id, depth);
-- The index on search_name is created by _migrate_schema, which is where that
-- column is added; it cannot be indexed before it exists.

-- Keyed by (drive_id, rel_path), not node id: nodes are wiped and reinserted
-- on every rescan so their ids are not stable, but paths are. Tags survive a
-- rescan unless the title folder is renamed or moved.
CREATE TABLE IF NOT EXISTS tags (
    drive_id  TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    tag       TEXT NOT NULL,
    PRIMARY KEY (drive_id, rel_path, tag)
);
"""


# Set by dashboard.py to a callable returning the connection for the request
# in flight, so a page render opens one instead of dozens. Left None outside a
# request, where every caller gets its own as before.
request_connection = None


def get_conn():
    """
    A connection with the per-connection pragmas set.

    Inside a web request this hands back the one already open, so the callers
    below can keep opening and closing freely without paying for it. Closing a
    shared connection is a no-op; see _Shared.

    journal_mode is not among the pragmas: WAL is a property of the file, set
    once by init_db, and asking for it on every connect takes a lock for
    nothing.
    """
    if request_connection is not None:
        shared = request_connection()
        if shared is not None:
            return shared
    return new_conn()


def new_conn():
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Lets a blocked writer wait its turn rather than failing instantly.
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class SharedConnection:
    """
    A connection that ignores close().

    Every function here opens and closes its own connection, which is the
    right shape for a script and wasteful for a page render that calls twenty
    of them. Wrapping one connection so close() does nothing lets the request
    own its lifetime without rewriting each caller.
    """

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        pass

    def real_close(self):
        self._conn.close()


# Columns added after the first release, since SQLite has no ADD COLUMN IF
# NOT EXISTS. For a folder, modified_at is the newest file anywhere inside,
# which is what tells a show still receiving episodes from a settled one.
ADDED_NODE_COLUMNS = {
    "created_at": "REAL",    # unix time, when it first appeared on the drive
    "modified_at": "REAL",   # unix time, newest content anywhere inside it
    # name with release noise and punctuation stripped, so a search matches
    # how a title is written rather than how it is spelled. Season markers
    # are kept and canonicalised, since "Season 2" is part of the real title.
    # See matching.title_key.
    "search_name": "TEXT",
}

ADDED_DRIVE_COLUMNS = {
    # How this drive was last scanned, so a rescan repeats those options
    # rather than falling back to the defaults.
    "root_prefix": "TEXT",
    "redundancy_prefix": "TEXT",
    "redundancy_include": "TEXT",
    # The letter this drive was last seen on, e.g. "L:". Kept apart from the
    # label so the label stays a plain name: a letter baked into the name goes
    # stale, and means nothing on another machine. Shown beside the label
    # wherever knowing which drive to reach for actually helps.
    "last_letter": "TEXT",
    # A drivetypes value, or NULL to read it off the volume label each time.
    # A value here was set by hand and always wins.
    "drive_type": "TEXT",
    # Filled on purpose and read only, so the low space warning is suppressed.
    "cold_storage": "INTEGER NOT NULL DEFAULT 0",
}


# Matches a drive letter in brackets at the end of a label, which is how
# auto-detected names used to be built, e.g. "Toshiba (L:)".
BAKED_LETTER = re.compile(r"\s*\(\s*([A-Za-z]):?\\?\s*\)\s*$")


def _split_baked_letter(conn):
    """
    Move a drive letter out of any label that still has one baked in.

    Old auto labels were built as "name (D:)", which goes stale the moment
    Windows hands that letter to something else, and means nothing at all on
    another machine. The letter is kept in last_letter instead.
    """
    for row in conn.execute("SELECT drive_id, label, last_letter FROM drives"):
        match = BAKED_LETTER.search(row["label"] or "")
        if not match:
            continue
        conn.execute(
            "UPDATE drives SET label = ?, last_letter = COALESCE(last_letter, ?) "
            "WHERE drive_id = ?",
            (BAKED_LETTER.sub("", row["label"]).strip(),
             match.group(1).upper() + ":", row["drive_id"]),
        )


def _migrate_schema(conn):
    """Add any columns missing from an older database file."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(drives)")}
    added = []
    for column, coltype in ADDED_DRIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE drives ADD COLUMN {column} {coltype}")
            added.append(column)

    existing = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)")}
    for column, coltype in ADDED_NODE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {column} {coltype}")

    if "last_letter" in added:
        _split_baked_letter(conn)
    # Backfill in one pass rather than making search cope with NULLs.
    missing = conn.execute(
        "SELECT id, name FROM nodes WHERE search_name IS NULL").fetchall()
    if missing:
        conn.executemany(
            "UPDATE nodes SET search_name = ? WHERE id = ?",
            [(matching.title_key(r["name"]), r["id"]) for r in missing])
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_search ON nodes(search_name)")


def init_db():
    conn = get_conn()
    # WAL lets the dashboard read while a scan writes. It sticks to the file,
    # so this is the only place that has to ask for it.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_drive(drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path,
                 scan_options=None, last_letter=None):
    """Record a drive's capacity and label after a scan. scan_options is
    remembered for later rescans; None leaves what is stored alone."""
    opts = scan_options or {}
    conn = get_conn()
    # A letter belongs to one drive at a time. Whoever was last seen here has
    # moved or gone, so their claim on it is stale and is dropped.
    if last_letter:
        conn.execute(
            "UPDATE drives SET last_letter = NULL WHERE last_letter = ? AND drive_id != ?",
            (last_letter, drive_id),
        )
    conn.execute(
        """
        INSERT INTO drives (drive_id, label, total_bytes, used_bytes, free_bytes,
                            last_seen_path, last_scanned, last_letter,
                            root_prefix, redundancy_prefix, redundancy_include)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE SET
            label=excluded.label,
            total_bytes=excluded.total_bytes,
            used_bytes=excluded.used_bytes,
            free_bytes=excluded.free_bytes,
            last_seen_path=excluded.last_seen_path,
            last_scanned=excluded.last_scanned,
            last_letter=COALESCE(excluded.last_letter, drives.last_letter),
            root_prefix=COALESCE(excluded.root_prefix, drives.root_prefix),
            redundancy_prefix=COALESCE(excluded.redundancy_prefix, drives.redundancy_prefix),
            redundancy_include=COALESCE(excluded.redundancy_include, drives.redundancy_include)
        """,
        (drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path, now_iso(),
         last_letter,
         opts.get("root_prefix"), opts.get("redundancy_prefix"), opts.get("redundancy_include")),
    )
    conn.commit()
    conn.close()


def get_drive(drive_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM drives WHERE drive_id = ?", (drive_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def replace_nodes(drive_id, node_list):
    """
    Replace this drive's whole tree, remapping scanner.py's tmp_id links to
    real row ids as it goes. Tags live in their own table and are untouched.
    """
    conn = get_conn()
    conn.execute("DELETE FROM nodes WHERE drive_id = ?", (drive_id,))

    tmp_to_real = {}
    cur = conn.cursor()
    for n in node_list:
        real_parent_id = tmp_to_real.get(n["parent_tmp_id"]) if n["parent_tmp_id"] is not None else None
        cur.execute(
            """
            INSERT INTO nodes (drive_id, parent_id, name, rel_path, is_dir, size_bytes,
                               depth, root_type, created_at, modified_at, search_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (drive_id, real_parent_id, n["name"], n["rel_path"], int(n["is_dir"]), n["size_bytes"],
             n["depth"], n.get("root_type", "library"),
             n.get("created_at"), n.get("modified_at"), matching.title_key(n["name"])),
        )
        tmp_to_real[n["tmp_id"]] = cur.lastrowid

    conn.commit()
    conn.close()


def get_all_drives():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM drives ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


NODE_ORDER = "ORDER BY is_dir DESC, size_bytes DESC"

# A title is at depth 2. Down to there, size order answers the question the
# tree is for: what is worth moving or deleting in one piece.
TITLE_DEPTH = 2


def _natural_key(name):
    """Sort key where digit runs compare as numbers, so Season 2 comes before
    Season 10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def _order_children(kids):
    """Orders one level of siblings. Inside a title the contents are a
    sequence, seasons and episodes, and a sequence is only readable in its own
    order, so those sort by name instead of by size."""
    if kids and kids[0]["depth"] > TITLE_DEPTH:
        kids.sort(key=lambda k: (not k["is_dir"], _natural_key(k["name"])))
    return kids


def _nest(rows):
    """Turns a flat row list into nested 'children' lists. Any node whose
    parent isn't in the row set is treated as a root of this partial tree."""
    by_id = {}
    for r in rows:
        d = dict(r)
        d["children"] = []
        d["has_children"] = False
        by_id[d["id"]] = d

    roots = []
    for d in by_id.values():
        parent = by_id.get(d["parent_id"]) if d["parent_id"] is not None else None
        if parent is None:
            roots.append(d)
        else:
            parent["children"].append(d)
            parent["has_children"] = True
    return roots, by_id


def get_tree_for_drive(drive_id, max_depth=2):
    """
    Roots, categories and titles, nested, stopping at max_depth.

    Depths 0-2 hold everything interactive and are a tiny slice of the tree.
    Seasons and files below that are fetched by get_children() on expand, so
    the initial page stays small however many files are indexed. Each node
    carries 'has_children' so the UI can draw an arrow for what it has not
    loaded.
    """
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE drive_id = ? AND depth <= ? {NODE_ORDER}",
        (drive_id, max_depth),
    ).fetchall()

    roots, by_id = _nest(rows)

    # Nodes at the cutoff have no children in this result set, so ask
    # directly, or a title with seasons inside would render as a leaf.
    edge_ids = [d["id"] for d in by_id.values() if d["depth"] == max_depth and d["is_dir"]]
    if edge_ids:
        placeholders = ",".join("?" * len(edge_ids))
        deeper = conn.execute(
            f"SELECT DISTINCT parent_id FROM nodes WHERE parent_id IN ({placeholders})",
            edge_ids,
        ).fetchall()
        for r in deeper:
            by_id[r["parent_id"]]["has_children"] = True

    conn.close()
    return roots


def get_children(parent_id):
    """One level of children, flat, each carrying 'has_children' so the UI can
    draw an arrow for grandchildren it has not fetched."""
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE parent_id = ? {NODE_ORDER}", (parent_id,)
    ).fetchall()

    kids = [dict(r) for r in rows]
    dir_ids = [k["id"] for k in kids if k["is_dir"]]
    with_kids = set()
    if dir_ids:
        placeholders = ",".join("?" * len(dir_ids))
        with_kids = {
            r["parent_id"]
            for r in conn.execute(
                f"SELECT DISTINCT parent_id FROM nodes WHERE parent_id IN ({placeholders})",
                dir_ids,
            ).fetchall()
        }
    conn.close()

    for k in kids:
        k["has_children"] = k["id"] in with_kids
        k["children"] = []
    return _order_children(kids)


def get_node(node_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_drive_usage(drive_id, total_bytes, used_bytes, free_bytes):
    """Refresh a drive's capacity without touching anything else. Used after
    a delete, so the free space shown is right without a full rescan."""
    conn = get_conn()
    conn.execute(
        "UPDATE drives SET total_bytes = ?, used_bytes = ?, free_bytes = ? WHERE drive_id = ?",
        (total_bytes, used_bytes, free_bytes, drive_id),
    )
    conn.commit()
    conn.close()


def delete_node_subtree(node_id):
    """
    Remove a node, its subtree and their tags, then subtract the freed size
    from every ancestor. Folder sizes are recursive totals, so an ancestor
    left alone would keep counting bytes that no longer exist.
    """
    conn = get_conn()
    try:
        node = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if node is None:
            return {"nodes": 0, "tags": 0, "size_bytes": 0}

        subtree = conn.execute(
            """
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM nodes WHERE id = ?
                UNION ALL
                SELECT nodes.id FROM nodes JOIN sub ON nodes.parent_id = sub.id
            )
            SELECT id FROM sub
            """,
            (node_id,),
        ).fetchall()
        ids = [r["id"] for r in subtree]
        placeholders = ",".join("?" * len(ids))

        # Tags are keyed by path, so clear this node's and anything below it.
        rel_prefix = node["rel_path"] + os.sep
        tag_cur = conn.execute(
            "DELETE FROM tags WHERE drive_id = ? AND (rel_path = ? OR rel_path LIKE ?)",
            (node["drive_id"], node["rel_path"], rel_prefix.replace("\\", "\\") + "%"),
        )
        tags_removed = tag_cur.rowcount

        conn.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", ids)

        # Correct every ancestor's recursive size.
        freed = node["size_bytes"]
        parent_id = node["parent_id"]
        while parent_id is not None:
            conn.execute("UPDATE nodes SET size_bytes = MAX(0, size_bytes - ?) WHERE id = ?",
                         (freed, parent_id))
            row = conn.execute("SELECT parent_id FROM nodes WHERE id = ?", (parent_id,)).fetchone()
            parent_id = row["parent_id"] if row else None

        conn.commit()
        return {"nodes": len(ids), "tags": tags_removed, "size_bytes": freed}
    finally:
        conn.close()


def get_drive_id_for_node(node_id):
    """Used to validate that a lazy-load request targets a real node."""
    conn = get_conn()
    row = conn.execute("SELECT drive_id FROM nodes WHERE id = ?", (node_id,)).fetchone()
    conn.close()
    return row["drive_id"] if row else None


def locate_node(node_id):
    """Everything needed to expand the tree down to a node: its drive, path,
    and ancestors outermost first."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        conn.close()
        return None

    parent_of = {
        r["id"]: r["parent_id"]
        for r in conn.execute("SELECT id, parent_id FROM nodes WHERE drive_id = ?",
                              (row["drive_id"],))
    }
    conn.close()

    chain = []
    pid = parent_of.get(row["id"])
    while pid is not None:
        chain.append(pid)
        pid = parent_of.get(pid)

    return {
        "id": row["id"],
        "drive_id": row["drive_id"],
        "rel_path": row["rel_path"],
        "name": row["name"],
        "ancestors": list(reversed(chain)),
    }


def get_largest_folders(drive_id, limit=8, min_depth=2):
    """
    What is eating this drive's space, at title level or below.

    A title is listed once: once a folder is selected none of its descendants
    are, since deleting the title deletes them anyway.
    """
    conn = get_conn()
    # Only the biggest few can win, and each one rules out its own subtree, so
    # reading limit * a small factor is always enough. Reading every directory
    # row on the drive to return six was the wasteful part.
    candidates = conn.execute(
        """
        SELECT * FROM nodes
        WHERE drive_id = ? AND is_dir = 1 AND depth >= ?
        ORDER BY size_bytes DESC, depth ASC
        LIMIT ?
        """,
        (drive_id, min_depth, limit * 8),
    ).fetchall()
    if not candidates:
        conn.close()
        return []

    # Ancestor chains only for the rows in hand, rather than the whole drive.
    parent_of, depth_of = {}, {}
    wanted = {r["parent_id"] for r in candidates if r["parent_id"] is not None}
    seen = set()
    while wanted:
        placeholders = ",".join("?" * len(wanted))
        rows = conn.execute(
            f"SELECT id, parent_id, depth FROM nodes WHERE id IN ({placeholders})",
            list(wanted),
        ).fetchall()
        seen |= wanted
        wanted = set()
        for r in rows:
            parent_of[r["id"]] = r["parent_id"]
            depth_of[r["id"]] = r["depth"]
            # Stop climbing once above title level, which is where the walk
            # below stops looking anyway.
            if r["parent_id"] is not None and r["depth"] > min_depth \
                    and r["parent_id"] not in seen:
                wanted.add(r["parent_id"])
    conn.close()

    selected = []
    selected_ids = set()
    for row in candidates:
        n = dict(row)
        pid = n["parent_id"]
        descendant_of_selected = False
        while pid is not None:
            if depth_of.get(pid, -1) < min_depth:
                break  # walked up past title level into category or root
            if pid in selected_ids:
                descendant_of_selected = True
                break
            pid = parent_of.get(pid)
        if descendant_of_selected:
            continue
        selected.append(n)
        selected_ids.add(n["id"])
        if len(selected) >= limit:
            break

    return selected


def search_nodes(query, limit=200):
    """
    Search names across every drive, matching how titles are actually written.

    Two passes. The index carries a normalised form of every name, so
    "prison break" reaches "Prison.Break.S01E03.1080p" and every word of the
    query has to be present. A plain substring match runs alongside it, which
    is what keeps a search for a release group or a year working.

    Each result carries enough to stand on its own in the result list, plus
    its ancestor ids so clicking it can expand the tree straight there.
    """
    conn = get_conn()
    wanted = matching.title_key(query).split()

    clauses = ["nodes.name LIKE ?"]
    params = [f"%{query}%"]
    for word in wanted[:6]:          # a long query is already selective
        clauses.append("nodes.search_name LIKE ?")
        params.append(f"%{word}%")
    # Substring OR (every normalised word), which is the union of the two
    # passes without needing two queries.
    where = f"({clauses[0]})"
    if len(clauses) > 1:
        where += " OR (" + " AND ".join(clauses[1:]) + ")"

    rows = conn.execute(
        f"""
        SELECT nodes.*, drives.label as drive_label,
               drives.last_letter as drive_letter_last
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE {where}
        ORDER BY (nodes.depth = 2) DESC, nodes.size_bytes DESC
        LIMIT ?
        """,
        params + [limit],
    ).fetchall()
    results = [dict(r) for r in rows]
    if not results:
        conn.close()
        return results

    # One parent map for the lot, rather than a query per row.
    drive_ids = {r["drive_id"] for r in results}
    placeholders = ",".join("?" * len(drive_ids))
    parent_of = {
        r["id"]: r["parent_id"]
        for r in conn.execute(
            f"SELECT id, parent_id FROM nodes WHERE drive_id IN ({placeholders})", list(drive_ids)
        )
    }

    tags_by_drive = {}
    for r in conn.execute("SELECT drive_id, rel_path, tag FROM tags"):
        tags_by_drive.setdefault((r["drive_id"], r["rel_path"]), []).append(r["tag"])
    conn.close()

    for r in results:
        chain = []
        pid = parent_of.get(r["id"])
        while pid is not None:
            chain.append(pid)
            pid = parent_of.get(pid)
        r["ancestors"] = list(reversed(chain))  # outermost first, to expand in order
        r["tags"] = tags_by_drive.get((r["drive_id"], r["rel_path"]), [])
    return results


def set_tag_bulk(items, tag, action):
    """Apply or remove one tag across many titles in one transaction. items is
    an iterable of (drive_id, rel_path). Returns how many were touched."""
    tag = (tag or "").strip()
    pairs = [(d, p) for d, p in items if d and p]
    if not tag or not pairs:
        return 0

    conn = get_conn()
    if action == "add":
        conn.executemany(
            "INSERT OR IGNORE INTO tags (drive_id, rel_path, tag) VALUES (?, ?, ?)",
            [(d, p, tag) for d, p in pairs],
        )
    elif action == "remove":
        conn.executemany(
            "DELETE FROM tags WHERE drive_id = ? AND rel_path = ? AND tag = ?",
            [(d, p, tag) for d, p in pairs],
        )
    else:
        conn.close()
        raise ValueError("action must be 'add' or 'remove'")
    conn.commit()
    conn.close()
    return len(pairs)


def copies_of(drive_id, rel_path):
    """
    Every other place this title exists, as (drive_id, rel_path).

    Matched the same way the shield matches, so what counts as "the same
    title" is one rule rather than two that can disagree.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT name FROM nodes WHERE drive_id = ? AND rel_path = ? AND depth = 2",
        (drive_id, rel_path),
    ).fetchone()
    if row is None:
        conn.close()
        return []

    key = matching.title_key(row["name"]) or row["name"].strip().lower()
    others = conn.execute(
        "SELECT drive_id, rel_path, name FROM nodes WHERE depth = 2 AND search_name = ?",
        (key,),
    ).fetchall()
    conn.close()
    return [(r["drive_id"], r["rel_path"]) for r in others
            if not (r["drive_id"] == drive_id and r["rel_path"] == rel_path)]


def prune_orphan_tags(drive_id):
    """
    Drop tags on paths this drive no longer has, and return how many went.

    Only ever called straight after a successful scan of that one drive, so
    "not in nodes" means the scan looked and the path was genuinely gone. A
    drive that is merely unplugged keeps every node and every tag: forgetting
    a drive is a separate, deliberate act.
    """
    conn = get_conn()
    cur = conn.execute(
        """
        DELETE FROM tags
        WHERE drive_id = ?
          AND rel_path NOT IN (SELECT rel_path FROM nodes WHERE drive_id = ?)
        """,
        (drive_id, drive_id),
    )
    removed = cur.rowcount
    conn.commit()
    conn.close()
    return removed


def clear_tags_for_drive(drive_id):
    """Drop every tag on a drive. Used by the category-rename migration, where
    the old tag rows are keyed to paths that no longer exist."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM tags WHERE drive_id = ?", (drive_id,))
    conn.commit()
    removed = cur.rowcount
    conn.close()
    return removed


def get_duplicate_map():
    """
    Titles that appear in more than one place, by matching name at depth 2.
    Returns {node_id: [other copies]}.

    Single-file titles count too. Leaving them out meant a film kept as one
    .mkv showed no shield however many copies of it existed, while the rest
    of the app could see them perfectly well.

    Worked out fresh each call rather than stored, so a deleted copy drops
    out on the next scan with no cleanup step.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.id, nodes.name, nodes.rel_path, nodes.drive_id, nodes.root_type,
               nodes.size_bytes, drives.label as drive_label,
               drives.last_letter as drive_letter_last
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.depth = 2
        """
    ).fetchall()
    conn.close()

    # Grouped on the normalised name, so "Better Call Paul" and
    # "Better.Call.Paul.1080p.x265-RARBG" are recognised as one title. Season
    # markers are deliberately kept: without them one season would be reported
    # as a backup of another, which is the worst mistake this could make.
    groups = {}
    for r in rows:
        key = matching.title_key(r["name"]) or r["name"].strip().lower()
        groups.setdefault(key, []).append(dict(r))

    # Child names for titles with a twin, so a partial copy reads as
    # "3 of 30 items" rather than a size percentage.
    candidate_ids = [i["id"] for items in groups.values() if len(items) > 1 for i in items]
    children_of = {i: set() for i in candidate_ids}
    if candidate_ids:
        conn = get_conn()
        placeholders = ",".join("?" * len(candidate_ids))
        for r in conn.execute(
            f"SELECT parent_id, name FROM nodes WHERE parent_id IN ({placeholders})",
            candidate_ids,
        ):
            children_of[r["parent_id"]].add(r["name"].strip().lower())
        conn.close()

    dup_map = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        for item in items:
            others = []
            for o in items:
                if o["id"] == item["id"]:
                    continue
                others.append({
                    "id": o["id"],
                    "drive_id": o["drive_id"],
                    "drive_label": o["drive_label"],
                    "drive_letter_last": o["drive_letter_last"],
                    "rel_path": o["rel_path"],
                    "root_type": o["root_type"],
                    "size_bytes": o["size_bytes"],
                    "same_drive": o["drive_id"] == item["drive_id"],
                    **compare_copies(item, o, children_of),
                })
            dup_map[item["id"]] = others
    return dup_map


# A copy within this fraction of the original counts as complete. Filesystem
# overhead and slightly different encodes make an exact match unrealistic.
FULL_COPY_SIZE_RATIO = 0.98


def compare_copies(item, other, children_of):
    """
    How does `other` relate to `item`? Compares child names, falling back to
    size for single-file titles.

      full     other has everything this copy has
      partial  some of it, usually a deliberate choice
      split    nothing shared. Not copies at all but different parts of one
               title, so deleting either loses something. Never protection.
    """
    mine = children_of.get(item["id"], set())
    theirs = children_of.get(other["id"], set())

    if mine and theirs:
        shared = len(mine & theirs)
        if shared == 0:
            return {"relation": "split", "complete": False,
                    "detail": f"no overlap, {len(theirs)} other items"}
        if shared >= len(mine):
            return {"relation": "full", "complete": True,
                    "detail": f"all {len(mine)} items"}
        return {"relation": "partial", "complete": False,
                "detail": f"{shared} of {len(mine)} items"}

    my_size = item["size_bytes"] or 0
    their_size = other["size_bytes"] or 0
    if my_size <= 0:
        return {"relation": "full", "complete": True, "detail": ""}
    ratio = their_size / my_size
    if ratio >= FULL_COPY_SIZE_RATIO:
        return {"relation": "full", "complete": True, "detail": ""}
    return {"relation": "partial", "complete": False,
            "detail": f"{round(100 * ratio)}% of the size"}


def redundancy_summary(drive_id):
    """
    What is actually in this drive's redundancy folder, counted in titles
    rather than folders. The folder itself is often present but empty, so its
    existence alone says nothing.
    """
    return redundancy_summaries().get(
        drive_id, {"has_root": False, "titles": 0, "size_bytes": 0})


def redundancy_summaries():
    """
    The same figures for every drive at once, in two queries.

    Worth having separately because the suggestion ranking asks for this per
    candidate, and one connection per drive per candidate is most of the work
    in a build.
    """
    conn = get_conn()
    roots = {r["drive_id"] for r in conn.execute(
        "SELECT DISTINCT drive_id FROM nodes "
        "WHERE root_type = 'redundancy' AND depth = 0")}
    # Grouped by category as well, since what a drive holds is weighted by
    # kind: losing a drive full of shows costs more than one full of anime.
    rows = conn.execute(
        "SELECT n.drive_id, p.name AS category, COUNT(*) AS titles, "
        "       COALESCE(SUM(n.size_bytes), 0) AS bytes "
        "FROM nodes n JOIN nodes p ON n.parent_id = p.id "
        "WHERE n.root_type = 'redundancy' AND n.depth = 2 "
        "GROUP BY n.drive_id, p.name").fetchall()
    conn.close()

    def blank(has_root):
        return {"has_root": has_root, "titles": 0, "size_bytes": 0,
                "buckets": {k: 0 for k in tagging.WEIGHT_BUCKET_KEYS}}

    out = {d: blank(True) for d in roots}
    for r in rows:
        entry = out.setdefault(r["drive_id"], blank(False))
        entry["titles"] += r["titles"]
        entry["size_bytes"] += r["bytes"]
        entry["buckets"][tagging.weight_bucket_for_category(r["category"])] += r["titles"]
    return out


def indexed_bytes_by_drive():
    """
    How much of each drive is media MediaVault knows about, kept apart as the
    library and the copies in redundancy folders.

    Sums the root folders only, since a folder's size is already the recursive
    total of everything inside it. Covers both roots, so what is left over is
    genuinely everything else on the drive.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT drive_id, root_type, COALESCE(SUM(size_bytes), 0) AS bytes "
        "FROM nodes WHERE depth = 0 GROUP BY drive_id, root_type"
    ).fetchall()
    conn.close()

    out = {}
    for r in rows:
        bands = out.setdefault(r["drive_id"], {"library": 0, "redundancy": 0})
        # Anything not marked as a redundancy root counts as library.
        key = "redundancy" if r["root_type"] == "redundancy" else "library"
        bands[key] += r["bytes"]
    return out


def title_keys_on_drive(drive_id):
    """
    Every title already on a drive, keyed by normalised name.

    Matched the way duplicates are, so a differently named copy of the same
    show still counts. Used to keep a second copy off a drive that has one,
    since two copies on one disk protect nothing.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT name, rel_path, root_type FROM nodes WHERE drive_id = ? AND depth = 2",
        (drive_id,),
    ).fetchall()
    conn.close()

    out = {}
    for r in rows:
        key = matching.title_key(r["name"]) or r["name"].strip().lower()
        out.setdefault(key, []).append(
            {"rel_path": r["rel_path"], "root_type": r["root_type"]})
    return out


# Below this share of a drive's top-level library folders looking like real
# categories, the drive is laid out flat rather than merely having one odd
# folder name.
CATEGORY_SHARE_MIN = 0.5


def flat_layout_drives():
    """
    Drives whose library folder holds shows directly instead of categories.

    On such a drive every figure that depends on the convention is wrong:
    what MediaVault calls a title is really one season or episode, and what
    it calls a category is really the show. Detected by how few of the
    top-level folders look like a category at all, so one unusual name does
    not condemn a drive that is otherwise laid out properly.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.drive_id, nodes.name, drives.label AS drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.depth = 1 AND nodes.root_type = 'library' AND nodes.is_dir = 1
        """
    ).fetchall()
    conn.close()

    by_drive = {}
    for r in rows:
        entry = by_drive.setdefault(
            r["drive_id"], {"drive_id": r["drive_id"], "label": r["drive_label"],
                            "known": 0, "unknown": [], "total": 0})
        entry["total"] += 1
        if tagging.weight_bucket_for_category(r["name"]) == "other":
            entry["unknown"].append(r["name"])
        else:
            entry["known"] += 1

    return [e for e in by_drive.values()
            if e["total"] and e["known"] / e["total"] < CATEGORY_SHARE_MIN]


def library_sizes_by_weight_bucket(exclude_drive_ids=()):
    """
    Every library title's size, grouped by weight bucket.

    Feeds the Settings button that seeds the weights from your own library
    rather than from a guess about what a show or a film usually weighs.
    Drives laid out flat are excluded by the caller: their "titles" are
    episodes, and averaging those in would drag every median down.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.size_bytes, nodes.drive_id, parent.name AS category
        FROM nodes JOIN nodes AS parent ON nodes.parent_id = parent.id
        WHERE nodes.depth = 2 AND nodes.root_type = 'library'
        """
    ).fetchall()
    conn.close()

    skip = set(exclude_drive_ids)
    out = {k: [] for k in tagging.WEIGHT_BUCKET_KEYS}
    for r in rows:
        if r["drive_id"] in skip:
            continue
        out[tagging.weight_bucket_for_category(r["category"])].append(r["size_bytes"] or 0)
    return out


def copies_by_title_key():
    """
    Every copy of every title, grouped by normalised name.

    Covers single-file titles, which the duplicate map does not, so it is the
    honest answer to "how many copies of this are there, and which of them
    are backups".
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.id, nodes.name, nodes.rel_path, nodes.root_type,
               nodes.drive_id, drives.label AS drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.depth = 2
        """
    ).fetchall()
    conn.close()

    out = {}
    for r in rows:
        key = matching.title_key(r["name"]) or r["name"].strip().lower()
        out.setdefault(key, []).append(dict(r))
    return out


def library_title_keys():
    """
    Normalised names of every library title, on any drive.

    Covers single-file titles as well as folders, which the duplicate map
    deliberately does not, so it is the honest test of whether a backup still
    has an original somewhere.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM nodes WHERE depth = 2 AND root_type = 'library'"
    ).fetchall()
    conn.close()
    return {matching.title_key(r["name"]) or r["name"].strip().lower() for r in rows}


def library_copies_of(name, exclude_node_id=None):
    """
    Every library copy of a title, anywhere, matched on the normalised name.

    A backup with none left is an orphan: the copy is the only one there is,
    and it is no longer a copy of anything.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.id, nodes.rel_path, nodes.drive_id, drives.label AS drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.depth = 2 AND nodes.root_type = 'library'
        """
    ).fetchall()
    conn.close()

    want = matching.title_key(name) or name.strip().lower()
    out = []
    for r in rows:
        if exclude_node_id is not None and r["id"] == exclude_node_id:
            continue
        key = matching.title_key(r["rel_path"].rsplit(os.sep, 1)[-1])
        if (key or "") == want:
            out.append(dict(r))
    return out


def same_drive_duplicates():
    """
    Titles held as both a library copy and a backup on one drive.

    That protects nothing, since the drive failing takes both, and it wastes
    the space of the second copy. MediaVault refuses to create it, so any
    that turn up came in on a drive that already had them.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.name, nodes.rel_path, nodes.root_type, nodes.size_bytes,
               nodes.drive_id, drives.label AS drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.depth = 2
        """
    ).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        key = matching.title_key(r["name"]) or r["name"].strip().lower()
        groups.setdefault((r["drive_id"], key), []).append(dict(r))

    out = []
    for copies in groups.values():
        if len({c["root_type"] for c in copies}) < 2:
            continue
        backups = [c for c in copies if c["root_type"] == "redundancy"]
        out.append({
            "drive_id": copies[0]["drive_id"],
            "drive_label": copies[0]["drive_label"],
            "name": copies[0]["name"],
            "paths": [c["rel_path"] for c in copies],
            # Deleting the backup side is what reclaims the space.
            "wasted_bytes": sum(c["size_bytes"] or 0 for c in backups),
        })
    return sorted(out, key=lambda e: -e["wasted_bytes"])


def title_counts_by_category():
    """
    How many titles sit in each category, per drive, in one query.

    Counted from the folder a title lives in rather than its tags, so the
    figure matches what the tree shows even where tags have been edited.
    Returns {drive_id: {category_name: count}}.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT child.drive_id AS drive_id, parent.name AS category, COUNT(*) AS n
        FROM nodes child JOIN nodes parent ON child.parent_id = parent.id
        WHERE child.depth = 2 AND child.root_type = 'library'
        GROUP BY child.drive_id, parent.name
        """
    ).fetchall()
    conn.close()

    out = {}
    for r in rows:
        out.setdefault(r["drive_id"], {})[r["category"]] = r["n"]
    return out


def get_tags_for_drive(drive_id):
    """Returns {rel_path: [tag, tag, ...]} for every tagged title on a drive."""
    conn = get_conn()
    rows = conn.execute("SELECT rel_path, tag FROM tags WHERE drive_id = ?", (drive_id,)).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["rel_path"], []).append(r["tag"])
    return result


def add_tag(drive_id, rel_path, tag):
    tag = tag.strip()
    if not tag:
        return
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO tags (drive_id, rel_path, tag) VALUES (?, ?, ?)", (drive_id, rel_path, tag)
    )
    conn.commit()
    conn.close()


def remove_tag(drive_id, rel_path, tag):
    conn = get_conn()
    conn.execute(
        "DELETE FROM tags WHERE drive_id = ? AND rel_path = ? AND tag = ?", (drive_id, rel_path, tag)
    )
    conn.commit()
    conn.close()


def ensure_default_tags(drive_id, rel_path, default_tags):
    """Seeds defaults only where a title has no tags at all, so hand-set and
    hand-removed tags are never overwritten."""
    if not default_tags:
        return
    seed_default_tags_bulk(drive_id, [(rel_path, default_tags)])


def seed_default_tags_bulk(drive_id, path_tag_pairs):
    """
    Bulk ensure_default_tags for scan time, over one connection rather than
    one per title. path_tag_pairs is an iterable of (rel_path, [tag, ...]).
    """
    pairs = [(p, t) for p, t in path_tag_pairs if t]
    if not pairs:
        return

    conn = get_conn()
    already_tagged = {
        r["rel_path"]
        for r in conn.execute("SELECT DISTINCT rel_path FROM tags WHERE drive_id = ?", (drive_id,))
    }
    rows = [
        (drive_id, rel_path, tag.strip())
        for rel_path, tags in pairs
        if rel_path not in already_tagged
        for tag in tags
        if tag.strip()
    ]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO tags (drive_id, rel_path, tag) VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()


def get_all_distinct_tags():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    conn.close()
    return [r["tag"] for r in rows]


def set_drive_type(drive_id, drive_type):
    """Record how a drive should be treated. None goes back to reading it off
    the volume label."""
    conn = get_conn()
    conn.execute("UPDATE drives SET drive_type = ? WHERE drive_id = ?", (drive_type, drive_id))
    conn.commit()
    conn.close()


def set_drive_cold_storage(drive_id, cold_storage):
    """Kept separate from set_drive_type so toggling one never clears the
    other."""
    conn = get_conn()
    conn.execute("UPDATE drives SET cold_storage = ? WHERE drive_id = ?",
                 (1 if cold_storage else 0, drive_id))
    conn.commit()
    conn.close()


def set_drive_label(drive_id, label):
    conn = get_conn()
    conn.execute("UPDATE drives SET label = ? WHERE drive_id = ?", (label, drive_id))
    conn.commit()
    conn.close()


def delete_drive(drive_id):
    """
    Forget a drive completely: capacity row, indexed tree and tags.

    Tags go too, or rows pointing at a drive that no longer exists would
    build up. Surviving a rescan is separate: that calls replace_nodes, which
    only touches the tree.
    """
    conn = get_conn()
    conn.execute("DELETE FROM nodes  WHERE drive_id = ?", (drive_id,))
    conn.execute("DELETE FROM tags   WHERE drive_id = ?", (drive_id,))
    conn.execute("DELETE FROM drives WHERE drive_id = ?", (drive_id,))
    conn.commit()
    conn.close()
