"""
dashboard.py - Local web dashboard for MediaVault.

Run: py dashboard.py
Then open http://127.0.0.1:5151 in your browser.

Reads mediavault.db (written by scanner.py) and renders one page showing
every known drive, its capacity, a drill-down tree of everything on it,
tags, and duplicate/redundancy indicators.
"""

from flask import Flask, render_template, request, jsonify
import db

app = Flask(__name__)

LOW_SPACE_THRESHOLD = 0.15  # flag drives with less than 15% free


def human(n_bytes):
    """Picks whichever unit makes the number easiest to read - a 300MB file
    shows as '312.4 MB', not '0.3 GB'."""
    if n_bytes is None:
        return "-"
    n = float(n_bytes)
    for unit, size in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


def annotate_tree(nodes, drive_total_bytes, drive_id, tags_by_path, dup_map):
    """Adds display fields recursively: size_h, pct_of_drive, tags, duplicate info."""
    for n in nodes:
        n["size_h"] = human(n["size_bytes"])
        n["pct_of_drive"] = round(100 * n["size_bytes"] / drive_total_bytes, 2) if drive_total_bytes else 0
        n["drive_id"] = drive_id
        # Tags and duplicate info apply to depth-2 items
        if n["depth"] == 2:
            n["tags"] = tags_by_path.get(n["rel_path"], [])
            n["duplicates"] = dup_map.get(n["id"], []) if n["is_dir"] else []
        else:
            n["tags"] = []
            n["duplicates"] = []
        annotate_tree(n["children"], drive_total_bytes, drive_id, tags_by_path, dup_map)


@app.route("/")
def home():
    db.init_db()
    drives = db.get_all_drives()
    dup_map = db.get_duplicate_map()

    for d in drives:
        total = d["total_bytes"] or 1
        used = d["used_bytes"] or 0
        free = d["free_bytes"] or 0
        d["pct_used"] = round(100 * used / total, 1)
        d["low_space"] = (free / total) < LOW_SPACE_THRESHOLD
        d["total_h"] = human(total)
        d["used_h"] = human(used)
        d["free_h"] = human(free)
        d["segments_filled"] = round(40 * used / total)

        tags_by_path = db.get_tags_for_drive(d["drive_id"])
        tree = db.get_tree_for_drive(d["drive_id"])
        annotate_tree(tree, total, d["drive_id"], tags_by_path, dup_map)
        d["tree"] = tree

        largest = db.get_largest_folders(d["drive_id"], limit=6)
        for lf in largest:
            lf["size_h"] = human(lf["size_bytes"])
            lf["tags"] = tags_by_path.get(lf["rel_path"], [])
            lf["duplicates"] = dup_map.get(lf["id"], [])
        d["largest_folders"] = largest

    grand_total = sum(d["total_bytes"] or 0 for d in drives)
    grand_used = sum(d["used_bytes"] or 0 for d in drives)
    grand_free = sum(d["free_bytes"] or 0 for d in drives)

    low_space_drives = [d for d in drives if d["low_space"]]
    all_tags = db.get_all_distinct_tags()

    stats = {
        "drive_count": len(drives),
        "total_h": human(grand_total),
        "used_h": human(grand_used),
        "free_h": human(grand_free),
        "pct_used": round(100 * grand_used / grand_total, 1) if grand_total else 0,
    }

    return render_template(
        "dashboard.html",
        drives=drives,
        stats=stats,
        low_space_drives=low_space_drives,
        all_tags=all_tags,
    )


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = db.search_nodes(q) if q else []
    for r in results:
        r["size_h"] = human(r["size_bytes"])
    return jsonify({"results": results})


@app.route("/api/tag", methods=["POST"])
def api_tag():
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")
    rel_path = data.get("rel_path")
    tag = (data.get("tag") or "").strip()
    action = data.get("action", "add")

    if not drive_id or not rel_path or not tag:
        return jsonify({"ok": False, "error": "drive_id, rel_path, and tag are required"}), 400

    if action == "add":
        db.add_tag(drive_id, rel_path, tag)
    elif action == "remove":
        db.remove_tag(drive_id, rel_path, tag)
    else:
        return jsonify({"ok": False, "error": "action must be 'add' or 'remove'"}), 400

    return jsonify({"ok": True})


@app.route("/api/tags")
def api_tags():
    return jsonify({"tags": db.get_all_distinct_tags()})


if __name__ == "__main__":
    db.init_db()
    app.run(host="127.0.0.1", port=5151, debug=False)
