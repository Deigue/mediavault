"""
db.py - SQLite storage layer for MediaVault.

The database is the single source of truth: one file (mediavault.db) that
lives on your main PC. Every drive scan overwrites that drive's previous
snapshot, so the data always reflects the last time each drive was scanned.

"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediavault.db")

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

-- Tags are keyed by (drive_id, rel_path) rather than node id, because nodes
-- are wiped and re-inserted on every rescan (their autoincrement ids are not
-- stable) - but the same title's rel_path on the same drive usually is, so
-- tags survive rescans as long as you don't rename/move the title folder.
CREATE TABLE IF NOT EXISTS tags (
    drive_id  TEXT NOT NULL,
    rel_path  TEXT NOT NULL,
    tag       TEXT NOT NULL,
    PRIMARY KEY (drive_id, rel_path, tag)
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_drive(drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path):
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO drives (drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path, last_scanned)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE SET
            label=excluded.label,
            total_bytes=excluded.total_bytes,
            used_bytes=excluded.used_bytes,
            free_bytes=excluded.free_bytes,
            last_seen_path=excluded.last_seen_path,
            last_scanned=excluded.last_scanned
        """,
        (drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path, now_iso()),
    )
    conn.commit()
    conn.close()


def replace_nodes(drive_id, node_list):
    """
    node_list: list of dicts with keys: tmp_id, parent_tmp_id, name, rel_path,
    is_dir, size_bytes, depth, root_type - built by scanner.py in
    parent-before-child order.
    Wipes this drive's previous tree and inserts the new one, remapping
    tmp_id -> real autoincrement id as it goes so parent_id links are correct.
    Tags (kept in a separate table, keyed by rel_path) are left untouched.
    """
    conn = get_conn()
    conn.execute("DELETE FROM nodes WHERE drive_id = ?", (drive_id,))

    tmp_to_real = {}
    cur = conn.cursor()
    for n in node_list:
        real_parent_id = tmp_to_real.get(n["parent_tmp_id"]) if n["parent_tmp_id"] is not None else None
        cur.execute(
            """
            INSERT INTO nodes (drive_id, parent_id, name, rel_path, is_dir, size_bytes, depth, root_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (drive_id, real_parent_id, n["name"], n["rel_path"], int(n["is_dir"]), n["size_bytes"],
             n["depth"], n.get("root_type", "library")),
        )
        tmp_to_real[n["tmp_id"]] = cur.lastrowid

    conn.commit()
    conn.close()


def get_all_drives():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM drives ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tree_for_drive(drive_id):
    """Returns a list of root nodes (library folders), each with nested 'children'."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM nodes WHERE drive_id = ? ORDER BY is_dir DESC, size_bytes DESC", (drive_id,)
    ).fetchall()
    conn.close()

    by_id = {}
    roots = []
    for r in rows:
        d = dict(r)
        d["children"] = []
        by_id[d["id"]] = d

    for d in by_id.values():
        if d["parent_id"] is None:
            roots.append(d)
        else:
            parent = by_id.get(d["parent_id"])
            if parent:
                parent["children"].append(d)

    return roots


def get_largest_folders(drive_id, limit=8, min_depth=2):
    """
    'What's eating my space' list, restricted to title-level folders.

    depth 0 = the library root itself (e.g. "Videos")
    depth 1 = category folders ("01_Anime")
    depth 2 = titles ("Sword Mart Offline")

    A title is only shown once even if it has large subfolders (seasons,
    release-named folders, etc.) - if a folder is already selected, none
    of its descendants are also listed, since deleting the title deletes
    them anyway.
    """
    conn = get_conn()

    # Full parent/depth map for the drive, used to walk ancestor chains.
    all_rows = conn.execute(
        "SELECT id, parent_id, depth FROM nodes WHERE drive_id = ? AND is_dir = 1", (drive_id,)
    ).fetchall()
    parent_of = {r["id"]: r["parent_id"] for r in all_rows}
    depth_of = {r["id"]: r["depth"] for r in all_rows}

    candidates = conn.execute(
        """
        SELECT * FROM nodes
        WHERE drive_id = ? AND is_dir = 1 AND depth >= ?
        ORDER BY size_bytes DESC, depth ASC
        """,
        (drive_id, min_depth),
    ).fetchall()
    conn.close()

    selected = []
    selected_ids = set()
    for row in candidates:
        n = dict(row)
        pid = n["parent_id"]
        descendant_of_selected = False
        while pid is not None:
            if depth_of.get(pid, -1) < min_depth:
                break  # walked up past title-level into category/root - stop
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


def search_nodes(query):
    """Search folder/file names across every drive. Returns matches with drive label."""
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.*, drives.label as drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.name LIKE ?
        ORDER BY nodes.size_bytes DESC
        LIMIT 200
        """,
        (f"%{query}%",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_duplicate_map():
    """
    Finds titles (depth-2 folders) that appear in more than one physical
    location - whether that's a Videos library + a 99_Redundancy backup, two
    separate library drives, or anything else with a matching name.

    Returns: {node_id: [ {drive_label, rel_path, root_type}, ... other copies ]}

    Computed fresh from the current nodes table on every call (nodes are
    fully replaced on each scan), so if a duplicate is deleted from one
    drive, the next scan makes it vanish from this map automatically -
    no cleanup step needed.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.id, nodes.name, nodes.rel_path, nodes.drive_id, nodes.root_type,
               drives.label as drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.is_dir = 1 AND nodes.depth = 2
        """
    ).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        key = r["name"].strip().lower()
        groups.setdefault(key, []).append(dict(r))

    dup_map = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        for item in items:
            others = [
                {"drive_label": o["drive_label"], "rel_path": o["rel_path"], "root_type": o["root_type"]}
                for o in items if o["id"] != item["id"]
            ]
            dup_map[item["id"]] = others
    return dup_map


def get_tags_for_drive(drive_id):
    """Returns {rel_path: [tag, tag, ...]} for every tagged title on a drive."""
    conn = get_conn()
    rows = conn.execute("SELECT rel_path, tag FROM tags WHERE drive_id = ?", (drive_id,)).fetchall()
    conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["rel_path"], []).append(r["tag"])
    return result


def has_any_tags(drive_id, rel_path):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM tags WHERE drive_id = ? AND rel_path = ? LIMIT 1", (drive_id, rel_path)
    ).fetchone()
    conn.close()
    return row is not None


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
    """Only seeds default tags (see tagging.py) if this title has no tags at
    all yet, so it never overwrites tags you've set or removed by hand."""
    if not default_tags or has_any_tags(drive_id, rel_path):
        return
    for tag in default_tags:
        add_tag(drive_id, rel_path, tag)


def get_all_distinct_tags():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    conn.close()
    return [r["tag"] for r in rows]


def set_drive_label(drive_id, label):
    conn = get_conn()
    conn.execute("UPDATE drives SET label = ? WHERE drive_id = ?", (label, drive_id))
    conn.commit()
    conn.close()


def delete_drive(drive_id):
    conn = get_conn()
    conn.execute("DELETE FROM nodes WHERE drive_id = ?", (drive_id,))
    conn.execute("DELETE FROM drives WHERE drive_id = ?", (drive_id,))
    conn.commit()
    conn.close()
