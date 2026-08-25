"""
dashboard.py - Local web dashboard for MediaVault.

Run: py dashboard.py
Then open http://127.0.0.1:5151 in your browser.

Reads mediavault.db (written by scanner.py) and renders one page showing
every known drive, its capacity, a drill-down tree of everything on it,
tags, and duplicate/redundancy indicators.
"""

import os

from flask import Flask, render_template, request, jsonify
import db
import fileops
import scanner
import scanjob

app = Flask(__name__)
# Without this Jinja compiles the template once at first request and caches it,
# so edits to dashboard.html only appear after restarting the server. The cost
# is one stat() per render, which is nothing next to the DB work.
app.config["TEMPLATES_AUTO_RELOAD"] = True

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


# Tag that marks a title you have deliberately only backed up part of, so it
# shows as covered rather than nagging you about the missing pieces.
PARTIAL_OK_TAG = "partial-ok"


def backup_state(duplicates, tags):
    """
    'full', 'partial', 'split', or None for a title with nothing elsewhere.

    A title counts as fully backed up if any single copy is complete. A
    partial copy is usually a deliberate choice, so tagging the title
    `partial-ok` promotes it to full.

    'split' means the other location shares nothing with this one: the same
    title broken across drives rather than copied. That is not protection,
    so it never counts as backed up regardless of tags.
    """
    if not duplicates:
        return None
    if any(d["complete"] for d in duplicates):
        return "full"

    if all(d["relation"] == "split" for d in duplicates):
        return "split"

    if any(t.lower() == PARTIAL_OK_TAG for t in tags):
        return "full"
    return "partial"


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
            n["backup_state"] = backup_state(n["duplicates"], n["tags"])
        else:
            n["tags"] = []
            n["duplicates"] = []
            n["backup_state"] = None
        annotate_tree(n["children"], drive_total_bytes, drive_id, tags_by_path, dup_map)


@app.route("/api/children")
def api_children():
    """
    One level of a folder's contents, for lazy expansion of the tree.

    Only depths 0-2 are rendered into the initial page (that's where tags,
    duplicate badges and filtering live, and it's a small fraction of the
    nodes). Everything deeper - seasons, episodes, individual files - is
    fetched from here the first time its parent folder is opened.
    """
    try:
        parent_id = int(request.args.get("parent_id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "parent_id must be an integer"}), 400

    drive_id = db.get_drive_id_for_node(parent_id)
    if drive_id is None:
        return jsonify({"ok": False, "error": "no such node"}), 404

    drive = next((d for d in db.get_all_drives() if d["drive_id"] == drive_id), None)
    total = (drive["total_bytes"] if drive else 0) or 1

    children = db.get_children(parent_id)
    for c in children:
        c["size_h"] = human(c["size_bytes"])
        c["pct_of_drive"] = round(100 * c["size_bytes"] / total, 2)

    return jsonify({"ok": True, "children": children})


@app.route("/")
def home():
    db.init_db()
    drives = db.get_all_drives()
    dup_map = db.get_duplicate_map()
    connected = scanner.get_connected_drives()

    for d in drives:
        # Where this drive is right now, rather than where it was last scanned.
        mount = connected.get(d["drive_id"])
        d["connected"] = mount is not None
        d["current_letter"] = scanner.drive_letter_for(mount)

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
            lf["backup_state"] = backup_state(lf["duplicates"], lf["tags"])
        d["largest_folders"] = largest

    grand_total = sum(d["total_bytes"] or 0 for d in drives)
    grand_used = sum(d["used_bytes"] or 0 for d in drives)
    grand_free = sum(d["free_bytes"] or 0 for d in drives)

    low_space_drives = [d for d in drives if d["low_space"]]
    all_tags = db.get_all_distinct_tags()

    stats = {
        "connected_count": sum(1 for d in drives if d["connected"]),
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
    # Show the current drive letter next to the label for drives that are
    # plugged in, so a result you can actually go and open is obvious.
    connected = scanner.get_connected_drives()
    for r in results:
        r["size_h"] = human(r["size_bytes"])
        r["drive_letter"] = scanner.drive_letter_for(connected.get(r["drive_id"]))
        r["connected"] = r["drive_letter"] is not None
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


@app.route("/api/drive/label", methods=["POST"])
def api_drive_label():
    """Rename a drive. The drive_id is what everything is keyed on, so the
    label is purely cosmetic and safe to change at any time - including
    while the drive is disconnected."""
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")
    label = (data.get("label") or "").strip()

    if not drive_id or not label:
        return jsonify({"ok": False, "error": "drive_id and a non-empty label are required"}), 400
    if not any(d["drive_id"] == drive_id for d in db.get_all_drives()):
        return jsonify({"ok": False, "error": "no such drive"}), 404

    db.set_drive_label(drive_id, label)
    return jsonify({"ok": True, "label": label})


@app.route("/api/tag/bulk", methods=["POST"])
def api_tag_bulk():
    """Apply or remove one tag across a multi-selection of titles."""
    data = request.get_json(force=True)
    tag = (data.get("tag") or "").strip()
    action = data.get("action", "add")
    items = [(i.get("drive_id"), i.get("rel_path")) for i in (data.get("items") or [])]

    if not tag or not items:
        return jsonify({"ok": False, "error": "tag and a non-empty items list are required"}), 400

    try:
        count = db.set_tag_bulk(items, tag, action)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "count": count, "tag": tag, "action": action})


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """
    Start scanning every connected drive worth scanning.

    Returns straight away with the job, which the page then polls. A scan of
    a full drive takes minutes, so it cannot be done inside this request.
    """
    job, problem = scanjob.start()
    if job is None:
        # 409: either a scan is already running, or there is nothing to scan.
        return jsonify({"ok": False, "error": problem}), 409
    return jsonify({"ok": True, "job": job})


@app.route("/api/scan/status")
def api_scan_status():
    """Progress of the running scan, or the result of the last one."""
    return jsonify({"ok": True, "job": scanjob.status()})


@app.route("/api/scan/targets")
def api_scan_targets():
    """Which drives a scan would cover right now, for the confirm prompt."""
    targets = scanner.find_scannable_drives()
    return jsonify({"ok": True, "targets": [
        {"root": t["root"], "label": t.get("label") or t["root"], "reason": t["reason"]}
        for t in targets
    ]})


@app.route("/api/node/<int:node_id>/locate")
def api_node_locate(node_id):
    """Where a node sits, so the page can expand the tree down to it."""
    found = db.locate_node(node_id)
    if found is None:
        return jsonify({"ok": False, "error": "no such node"}), 404
    return jsonify({"ok": True, "node": found})


@app.route("/api/node/open", methods=["POST"])
def api_node_open():
    """Open a file in its default application, or a folder in the file manager."""
    data = request.get_json(force=True)
    try:
        node_id = int(data.get("node_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "node_id must be an integer"}), 400

    try:
        path = fileops.open_in_shell(node_id)
    except fileops.FileOpError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "path": path})


@app.route("/api/node/delete", methods=["POST"])
def api_node_delete():
    """
    Delete the file or folder behind a node.

    mode is 'bin' for the Recycle Bin, which is recoverable but does not free
    space until the bin is emptied, or 'permanent' to remove it outright.
    """
    data = request.get_json(force=True)
    try:
        node_id = int(data.get("node_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "node_id must be an integer"}), 400

    mode = data.get("mode")
    if mode not in ("bin", "permanent"):
        return jsonify({"ok": False, "error": "mode must be 'bin' or 'permanent'"}), 400

    try:
        result = fileops.delete_node(node_id, permanent=(mode == "permanent"))
    except fileops.FileOpError as e:
        return jsonify({"ok": False, "error": str(e)}), 409

    result["freed_h"] = human(result["bytes_freed"])
    return jsonify({"ok": True, "result": result})


@app.route("/api/nodes/delete", methods=["POST"])
def api_nodes_delete():
    """
    Delete several nodes in one go, for the multi-select bar.

    Each is attempted on its own and reported separately, so one failure
    (a drive unplugged partway, a file held open) does not hide the rest.
    """
    data = request.get_json(force=True)
    mode = data.get("mode")
    if mode not in ("bin", "permanent"):
        return jsonify({"ok": False, "error": "mode must be 'bin' or 'permanent'"}), 400

    try:
        node_ids = [int(i) for i in (data.get("node_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "node_ids must be integers"}), 400
    if not node_ids:
        return jsonify({"ok": False, "error": "Nothing selected."}), 400

    results, freed = [], 0
    for node_id in node_ids:
        try:
            r = fileops.delete_node(node_id, permanent=(mode == "permanent"))
            freed += r["bytes_freed"]
            results.append({"node_id": node_id, "ok": True, "name": r["name"]})
        except fileops.FileOpError as e:
            results.append({"node_id": node_id, "ok": False, "error": str(e)})

    succeeded = [r for r in results if r["ok"]]
    return jsonify({
        "ok": bool(succeeded),
        "results": results,
        "deleted": len(succeeded),
        "failed": len(results) - len(succeeded),
        "freed_h": human(freed),
    })


@app.route("/api/drives/setup-candidates")
def api_setup_candidates():
    """Connected drives that do not have the folder layout yet."""
    return jsonify({"ok": True, "candidates": scanner.find_setup_candidates()})


@app.route("/api/drive/scaffold", methods=["POST"])
def api_drive_scaffold():
    """
    Create the folder layout on a drive MediaVault already knows about.

    This is the button shown inside a drive whose tree is empty, for example
    after its library folders were deleted. Drives that are not in the
    database at all go through /api/drives/setup instead, which works from a
    root path since they have no id yet.
    """
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")
    if not drive_id:
        return jsonify({"ok": False, "error": "drive_id is required"}), 400

    drive = db.get_drive(drive_id)
    if drive is None:
        return jsonify({"ok": False, "error": "no such drive"}), 404

    mount = scanner.find_mounted_drive(drive_id, max_age=0)
    if mount is None:
        return jsonify({
            "ok": False,
            "error": f"'{drive['label']}' is not connected. Plug it in and reload.",
        }), 409

    try:
        created = scanner.create_library_structure(mount)
        scanner.scan_and_store(mount, log=lambda _m: None)
    except OSError as e:
        return jsonify({"ok": False, "error": f"Could not create folders on {mount}: {e}"}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": f"Folders created, but the rescan failed: {e}"}), 500

    return jsonify({"ok": True, "created": created, "mount": mount})


@app.route("/api/drives/setup", methods=["POST"])
def api_drives_setup():
    """
    Create the Videos/<category> layout on the chosen drives, then scan them
    so they appear in the dashboard straight away.

    Takes drive root paths rather than ids, because a drive with no library
    folder has never been scanned and so has no id in the database yet. Each
    root is checked against the list of drives actually connected, so a
    request cannot name an arbitrary path.
    """
    data = request.get_json(force=True)
    roots = data.get("roots") or []
    if not roots:
        return jsonify({"ok": False, "error": "Pick at least one drive."}), 400

    allowed = {c["root"] for c in scanner.find_setup_candidates()}
    results = []
    for root in roots:
        if root not in allowed:
            results.append({"root": root, "ok": False,
                            "error": "not a connected drive awaiting setup"})
            continue
        try:
            created = scanner.create_library_structure(root)
            scanner.scan_and_store(root, log=lambda _msg: None)
            results.append({"root": root, "ok": True, "created": created})
        except OSError as e:
            results.append({"root": root, "ok": False, "error": f"could not create folders: {e}"})
        except Exception as e:
            results.append({"root": root, "ok": False, "error": str(e)})

    return jsonify({"ok": any(r["ok"] for r in results), "results": results})


@app.route("/api/drive/delete", methods=["POST"])
def api_drive_delete():
    """Forget a drive entirely - its capacity row and its whole indexed tree.

    Only touches the database; nothing is deleted from the actual drive.
    Rescanning the drive re-adds it (and its tags come back, since those are
    keyed by drive_id + rel_path and are left in place here)."""
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")

    if not drive_id:
        return jsonify({"ok": False, "error": "drive_id is required"}), 400
    if not any(d["drive_id"] == drive_id for d in db.get_all_drives()):
        return jsonify({"ok": False, "error": "no such drive"}), 404

    db.delete_drive(drive_id)
    return jsonify({"ok": True})


# Override with MEDIAVAULT_HOST / MEDIAVAULT_PORT, which is handy for running
# a second copy while the usual one is already on 5151.
HOST = os.environ.get("MEDIAVAULT_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEDIAVAULT_PORT", "5151"))

if __name__ == "__main__":
    db.init_db()
    # Waitress is a proper server meant to stay up for months, unlike Flask's
    # built-in one, which is for development. It also gives a fixed pool of
    # worker threads rather than a new thread per connection, which matters
    # now that a scan can run in the background while you use the page.
    # If it isn't installed, fall back so the app still starts.
    try:
        from waitress import serve
    except ImportError:
        print("waitress not installed, using Flask's development server.")
        print("Install it with: py -m pip install -r requirements.txt")
        app.run(host=HOST, port=PORT, debug=False)
    else:
        print(f"MediaVault running on http://{HOST}:{PORT}")
        print("Press CTRL+C to quit")
        serve(app, host=HOST, port=PORT, threads=6)
