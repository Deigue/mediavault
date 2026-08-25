"""
db.py - SQLite storage layer for MediaVault.

One file (mediavault.db) that lives on your main PC and holds everything.
Each drive scan replaces that drive's previous snapshot, so the data always
reflects the last time each drive was scanned.

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
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the dashboard keep reading while a scan writes, instead of
    # both sides tripping over "database is locked". busy_timeout gives a
    # blocked writer 10s to wait its turn rather than failing instantly.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


# Columns added to `drives` after the first release, with their defaults.
# Each one records how a drive was last scanned, so a rescan started from the
# dashboard repeats those options instead of falling back to the defaults and
# quietly dropping folders from the index.
ADDED_DRIVE_COLUMNS = {
    "root_prefix": "TEXT",
    "redundancy_prefix": "TEXT",
    "redundancy_include": "TEXT",
}


def _migrate_schema(conn):
    """Add any columns missing from an older database file."""
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(drives)")}
    for column, coltype in ADDED_DRIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE drives ADD COLUMN {column} {coltype}")


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate_schema(conn)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_drive(drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path,
                 scan_options=None):
    """
    Record a drive's capacity and label after a scan.

    scan_options: optional dict with root_prefix, redundancy_prefix and
    redundancy_include, remembered so a later rescan can repeat them. Pass
    None to leave whatever is already stored untouched.
    """
    opts = scan_options or {}
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO drives (drive_id, label, total_bytes, used_bytes, free_bytes,
                            last_seen_path, last_scanned,
                            root_prefix, redundancy_prefix, redundancy_include)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(drive_id) DO UPDATE SET
            label=excluded.label,
            total_bytes=excluded.total_bytes,
            used_bytes=excluded.used_bytes,
            free_bytes=excluded.free_bytes,
            last_seen_path=excluded.last_seen_path,
            last_scanned=excluded.last_scanned,
            root_prefix=COALESCE(excluded.root_prefix, drives.root_prefix),
            redundancy_prefix=COALESCE(excluded.redundancy_prefix, drives.redundancy_prefix),
            redundancy_include=COALESCE(excluded.redundancy_include, drives.redundancy_include)
        """,
        (drive_id, label, total_bytes, used_bytes, free_bytes, last_seen_path, now_iso(),
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


NODE_ORDER = "ORDER BY is_dir DESC, size_bytes DESC"


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
    Returns the top of the drive's tree - roots, categories, and titles -
    each with nested 'children', stopping at max_depth.

    Depths 0-2 are where everything interactive lives (tags, duplicate
    badges, tag filtering), and they're a tiny slice of the tree; the bulk
    of the nodes are seasons and individual files below that. Those are
    fetched on demand by get_children() when the user expands a folder, so
    the initial page stays small no matter how many files are indexed.

    Each returned node carries 'has_children' so the UI knows whether to
    render an expand arrow for a folder it hasn't loaded yet.
    """
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE drive_id = ? AND depth <= ? {NODE_ORDER}",
        (drive_id, max_depth),
    ).fetchall()

    roots, by_id = _nest(rows)

    # Nodes at the cutoff depth have no children in this result set, so ask
    # the DB directly whether they have any deeper - otherwise a title with
    # seasons inside would render as a leaf and never offer to expand.
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
    """
    One level of children for a folder, for lazy expansion in the dashboard.

    Returns flat dicts (no nesting) each carrying 'has_children', so the UI
    can render an expand arrow for grandchildren it hasn't fetched yet.
    """
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
    return kids


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
    Remove a node, everything beneath it, and their tags, then subtract the
    freed size from every ancestor.

    This keeps the tree usable after a delete without waiting for a rescan.
    Folder sizes are stored as recursive totals, so an ancestor that is not
    corrected would keep counting bytes that no longer exist.

    Returns {'nodes': n, 'tags': n, 'size_bytes': n}.
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
    """
    Everything the UI needs to expand the tree down to a node: its drive, its
    path, and its ancestors outermost first. Used when jumping to the other
    copy of a title from the backup badge.
    """
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


def search_nodes(query, limit=200):
    """
    Search folder/file names across every drive.

    Each result carries what the dashboard's result row needs to stand on its
    own - owning drive, full relative path, size, any tags - plus the chain of
    ancestor ids, so clicking a result can expand the tree straight to it.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.*, drives.label as drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.name LIKE ?
        ORDER BY nodes.size_bytes DESC
        LIMIT ?
        """,
        (f"%{query}%", limit),
    ).fetchall()
    results = [dict(r) for r in rows]
    if not results:
        conn.close()
        return results

    # Ancestor chains, resolved from one parent map rather than a query per row.
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
        r["ancestors"] = list(reversed(chain))  # outermost first, for sequential expansion
        r["tags"] = tags_by_drive.get((r["drive_id"], r["rel_path"]), [])
    return results


def set_tag_bulk(items, tag, action):
    """
    Apply or remove one tag across many titles in a single transaction.

    items: iterable of (drive_id, rel_path). Returns the number of titles
    affected. Used by the dashboard's multi-select tagging.
    """
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
    Finds titles (depth-2 folders) that appear in more than one physical
    location - whether that's a Videos library + a 99_Redundancy backup, two
    separate library drives, or anything else with a matching name.

    Returns: {node_id: [ {drive_label, rel_path, root_type}, ... other copies ]}

    Worked out fresh from the nodes table on every call. Nodes are fully
    replaced on each scan, so deleting a duplicate from one drive removes it
    from this map on the next scan without any cleanup step.
    """
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT nodes.id, nodes.name, nodes.rel_path, nodes.drive_id, nodes.root_type,
               nodes.size_bytes, drives.label as drive_label
        FROM nodes JOIN drives ON nodes.drive_id = drives.drive_id
        WHERE nodes.is_dir = 1 AND nodes.depth = 2
        """
    ).fetchall()
    conn.close()

    groups = {}
    for r in rows:
        key = r["name"].strip().lower()
        groups.setdefault(key, []).append(dict(r))

    # Direct child names for every title that has a twin, so a partial copy
    # can be described as "3 of 30 seasons" rather than a size percentage.
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
    How does `other` relate to `item`? Returns relation, complete, detail.

    Compares direct children by name, which for a TV show means seasons, and
    falls back to size for titles that are a single file. Three outcomes:

      full    other contains everything this copy has, so this is backed up
      partial some of it is there, usually a deliberate choice to back up
              only the parts worth keeping
      split   nothing is shared. The two are not copies at all, they are
              different parts of the same title living on different drives
              (seasons 1-30 here, 31-33 there). Neither is redundant, and
              deleting either loses something, so this must not be shown as
              protection.
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
    if not default_tags:
        return
    seed_default_tags_bulk(drive_id, [(rel_path, default_tags)])


def seed_default_tags_bulk(drive_id, path_tag_pairs):
    """
    Bulk version of ensure_default_tags for scan time.

    path_tag_pairs: iterable of (rel_path, [tag, ...]).

    Same rule as ensure_default_tags - a title that already has any tag is
    left completely alone - but the whole drive is done over one connection
    and one transaction. Doing it per title meant opening and committing a
    fresh SQLite connection for every title on the drive.
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


def set_drive_label(drive_id, label):
    conn = get_conn()
    conn.execute("UPDATE drives SET label = ? WHERE drive_id = ?", (label, drive_id))
    conn.commit()
    conn.close()


def delete_drive(drive_id):
    """
    Forget a drive completely: its capacity row, its indexed tree, and its
    tags.

    Tags go too. They are keyed by (drive_id, rel_path), so keeping them
    would leave rows pointing at a drive that no longer exists, and those
    build up every time a drive is forgotten. This does not affect tags
    surviving a rescan, which is a separate thing: a rescan calls
    replace_nodes, which only replaces the tree and leaves tags alone.
    """
    conn = get_conn()
    conn.execute("DELETE FROM nodes  WHERE drive_id = ?", (drive_id,))
    conn.execute("DELETE FROM tags   WHERE drive_id = ?", (drive_id,))
    conn.execute("DELETE FROM drives WHERE drive_id = ?", (drive_id,))
    conn.commit()
    conn.close()


def delete_orphan_tags():
    """Remove tag rows whose drive is no longer in the database. Cleans up
    after drives forgotten before delete_drive started removing tags."""
    conn = get_conn()
    cur = conn.execute("DELETE FROM tags WHERE drive_id NOT IN (SELECT drive_id FROM drives)")
    conn.commit()
    removed = cur.rowcount
    conn.close()
    return removed
