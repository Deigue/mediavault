"""
dashboard.py - the Flask app and every HTTP endpoint.

    py dashboard.py            then open http://127.0.0.1:5151
    py dashboard.py --dev      auto-reloading second copy on 5152

Renders one page from mediavault.db: every drive, its capacity, a drill-down
tree, tags, and backup indicators.
"""

import gzip
import os
import subprocess
import sys
from datetime import datetime, timezone

from flask import Flask, g, has_request_context, render_template, request, jsonify
import backup
import config
import db
import matching
import drivetypes
import fileops
import moveops
import movejob
import scanner
import scanjob
import structure
import suggestions
import tagging
import webguard

app = Flask(__name__)
# Otherwise Jinja caches the compiled template and edits to dashboard.html
# need a restart. Costs one stat() per render, nothing beside the DB work.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# The page is mostly repeated markup, so it compresses to a fraction of its
# size. Worth it even over loopback: the browser spends less time reading the
# socket before it can start parsing.
GZIP_MIN_BYTES = 4096


def _request_connection():
    """
    One database connection per request, shared by every db.py call in it.

    Those functions each open and close their own, which suits a script but
    means a page render pays for twenty connects. The wrapper ignores close(),
    so nothing else had to change.
    """
    if not has_request_context():
        return None
    shared = getattr(g, "_db", None)
    if shared is None:
        shared = db.SharedConnection(db.new_conn())
        g._db = shared
    return shared


db.request_connection = _request_connection


def _close_request_connection():
    """Let go of the database mid-request. Only the move needs this, and only
    because a file cannot be removed on Windows while a handle is open."""
    shared = g.pop("_db", None) if has_request_context() else None
    if shared is not None:
        shared.real_close()


@app.before_request
def guard_request():
    return webguard.guard()


@app.teardown_request
def close_connection(_exc):
    _close_request_connection()


@app.after_request
def compress(response):
    if (response.direct_passthrough
            or response.status_code < 200 or response.status_code >= 300
            or "Content-Encoding" in response.headers
            or response.content_length is None
            or response.content_length < GZIP_MIN_BYTES
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()):
        return response

    data = gzip.compress(response.get_data(), compresslevel=6)
    response.set_data(data)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(data))
    response.headers.add("Vary", "Accept-Encoding")
    return response


def human(n_bytes):
    """Whichever unit reads best, so 300MB shows as '312.4 MB', not '0.3 GB'."""
    if n_bytes is None:
        return "-"
    n = float(n_bytes)
    for unit, size in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{int(n)} B"


PARTIAL_OK_TAG = tagging.PARTIAL_OK_TAG


def ago(iso_timestamp):
    """
    "34m ago", "yesterday", "4w ago". The exact stamp goes in the tooltip.

    A timestamp is precise and unreadable; what you actually want to know is
    whether this was recent enough to trust.
    """
    if not iso_timestamp:
        return "never"
    try:
        when = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return iso_timestamp[:16]
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    seconds = (datetime.now(timezone.utc) - when).total_seconds()
    if seconds < 0:
        return "just now"
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 2:
        return "an hour ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 2:
        return "yesterday"
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7
    if weeks < 5:
        return f"{int(weeks)}w ago"
    months = days / 30.4
    if months < 12:
        return f"{int(months)}mo ago"
    return f"{int(days / 365)}y ago"


def drive_letter(live_mount, remembered):
    """
    Which letter to show for a drive: where it is now, or where it last was.

    A disconnected drive still says "(L:)" because that is how you know which
    one to go and plug in. The letter is never part of the label, so it can
    stay accurate without the name going stale.
    """
    return scanner.drive_letter_for(live_mount) or remembered or None


def summarise_database(info):
    """"7 drives, 5,837 titles, 139 tags, last scanned 3d ago"."""
    def count(n, thing):
        return f"{n:,} {thing}" + ("" if n == 1 else "s")
    return (f"{count(info['drives'], 'drive')}, {count(info['titles'], 'title')}, "
            f"{count(info['tags'], 'tag')}, last scanned {ago(info['last_scanned'])}")


def db_path_question(target, answered):
    """
    What to ask before pointing the app at target, or None to just get on
    with it.

    Two things can need an answer: the location is a poor home for a database
    that is written constantly, and there is already one sitting there. Both
    go in one dialog, because being asked twice about one decision is worse
    than one longer question.
    """
    if answered:
        return None
    warning = config.db_path_warning(target)
    occupied = os.path.exists(target)
    existing = db.describe_database(target)
    if not occupied and not warning:
        return None

    body = [warning] if warning else []
    choices = []
    if existing:
        body.append(f"{target} already holds a MediaVault database.")
        choices.append({
            "value": "adopt",
            "label": "Use the one that is already there",
            "detail": f"It holds {summarise_database(existing)}. The database "
                      f"you are using now stays at {db.db_path()}, untouched.",
        })
    elif occupied:
        body.append(f"There is already a file at {target}, and it is not a "
                    f"MediaVault database.")

    mine = db.describe_database(db.db_path())
    if occupied:
        choices.append({
            "value": "replace",
            "label": "Put the one you are using now there instead",
            "detail": (f"Moves your {summarise_database(mine)}. " if mine else "")
                      + "What is there now is renamed out of the way rather "
                        "than deleted, so it can be put back.",
            "danger": True,
        })
    else:
        choices.append({"value": "move", "label": "Move it there anyway",
                        "danger": True})

    return {
        "field": "db_path_mode",
        "title": "There is already a database there" if existing else
                 ("There is already a file there" if occupied else
                  "Keep the database there?"),
        "body": "\n\n".join(body),
        "choices": choices,
    }


def summarise_counts(per_category):
    """
    {category: n} folded into the buckets shown in the UI.

    Returns {'total': n, 'parts': [(label, n), ...]} with the empty buckets
    dropped, so a drive holding only anime says so rather than listing zeros.
    """
    totals = {}
    for category, n in (per_category or {}).items():
        key, label = tagging.bucket_for_category(category)
        entry = totals.setdefault(key, {"label": label, "n": 0})
        entry["n"] += n

    order = [key for key, _label, _m in tagging.CATEGORY_BUCKETS]
    parts = [(tagging.count_label(totals[k]["label"], totals[k]["n"]), totals[k]["n"])
             for k in order if k in totals and totals[k]["n"]]
    return {"total": sum(n for _label, n in parts), "parts": parts}


def raw_backup_state(duplicates):
    """
    What the copies actually are: 'full', 'partial', 'split', or None.

    Split means the other location shares nothing with this one, which is not
    protection and never counts however it is tagged.
    """
    if not duplicates:
        return None
    if any(d["complete"] for d in duplicates):
        return "full"
    if all(d["relation"] == "split" for d in duplicates):
        return "split"
    return "partial"


def backup_state(duplicates, tags):
    """The state as shown, which promotes a partial copy to full when
    partial-ok says the missing parts are deliberate."""
    state = raw_backup_state(duplicates)
    if state == "partial" and any(t.lower() == PARTIAL_OK_TAG for t in tags):
        return "full"
    return state


def annotate_tree(nodes, drive_total_bytes, drive_id, tags_by_path, dup_map, hints,
                  connected=True, library_keys=frozenset()):
    """Adds display fields recursively: size_h, pct_of_drive, tags, duplicate
    info, layout hints."""
    for n in nodes:
        n["size_h"] = human(n["size_bytes"])
        n["pct_of_drive"] = round(100 * n["size_bytes"] / drive_total_bytes, 2) if drive_total_bytes else 0
        n["drive_id"] = drive_id
        # Moving and backing up both read the files, so both are dead while
        # the drive is unplugged, however much of its tree is still shown.
        n["drive_connected"] = connected
        # Tags, duplicates and hints all apply at title level.
        if n["depth"] == 2:
            n["tags"] = tags_by_path.get(n["rel_path"], [])
            n["duplicates"] = dup_map.get(n["id"], [])
            n["backup_state"] = backup_state(n["duplicates"], n["tags"])
            # The unpromoted state too, so removing partial-ok can put the
            # amber shield back without a reload.
            n["backup_real"] = raw_backup_state(n["duplicates"])
            n["hints"] = hints.get(n["id"], [])
            n["hint_detail"] = structure.describe(n["hints"])
            # A backup with no library copy left anywhere is no longer a copy
            # of anything, and is the only thing Promote acts on. Not read off
            # the duplicate map, which covers folders only, so a single-file
            # title would have read as an orphan while its original sat there.
            key = matching.title_key(n["name"]) or n["name"].strip().lower()
            n["is_orphan_backup"] = (
                n["root_type"] == "redundancy" and key not in library_keys
            )
        else:
            n["tags"] = []
            n["duplicates"] = []
            n["backup_state"] = None
            n["backup_real"] = None
            n["is_orphan_backup"] = False
            n["hints"] = []
            n["hint_detail"] = ""
        # A category folder says how many titles it holds. The children are
        # already loaded at this depth, so this costs no query.
        if n["depth"] == 1 and n["is_dir"]:
            n["title_count"] = len(n["children"])
            n["title_word"] = tagging.count_label(
                tagging.bucket_for_category(n["name"])[1], n["title_count"])
        else:
            n["title_count"] = None
        annotate_tree(n["children"], drive_total_bytes, drive_id, tags_by_path,
                      dup_map, hints, connected, library_keys)


@app.route("/api/children")
def api_children():
    """One level of a folder's contents, for lazy expansion. Depths 0-2 are
    rendered into the page; anything deeper is fetched from here."""
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
    hint_conn = db.get_conn()
    counts_by_drive = db.title_counts_by_category()
    media_by_drive = db.indexed_bytes_by_drive()
    # Worked out once: every title's promote state is a lookup against it.
    library_keys = db.library_title_keys()

    for d in drives:
        # Where this drive is right now, rather than where it was last scanned.
        mount = connected.get(d["drive_id"])
        d["connected"] = mount is not None
        d["current_letter"] = drive_letter(mount, d.get("last_letter"))
        # Only knowable while the drive is plugged in.
        d["external"] = scanner.is_external(mount) if mount else False
        d["bus_type"] = scanner.get_bus_type(mount) if mount else None

        total = d["total_bytes"] or 1
        used = d["used_bytes"] or 0
        free = d["free_bytes"] or 0
        d["pct_used"] = round(100 * used / total, 1)

        d["drive_type"] = drivetypes.detect(
            label=d["label"], removable=False, stored=d.get("drive_type"),
            bus_type=d["bus_type"],
        )
        # A stored value was set by hand rather than read off the label.
        d["type_is_manual"] = d.get("drive_type") in drivetypes.ALL_TYPES
        d["type_label"] = drivetypes.rule_for(d["drive_type"])["label"]
        d["cold_storage"] = bool(d.get("cold_storage"))
        d["allows_cold"] = drivetypes.allows_cold_storage(d["drive_type"])
        space = drivetypes.evaluate(d["drive_type"], free, total, d["cold_storage"])
        d["space"] = space
        d["low_space"] = space["low"]
        # Drives the border and the free figure. A drive with no capacity
        # figures has nothing to judge, so it stays neutral rather than
        # reading as critically full on a free percentage of zero.
        # Cold storage is kept full on purpose and its warnings are off, so
        # it stays neutral however little is left. evaluate() still calls it
        # critical below the 3% threshold, which is right for the message in
        # the card but wrong for a border that means "this needs attention".
        if not d["total_bytes"]:
            d["space_state"] = "unknown"
        elif d["cold_storage"] and d["allows_cold"]:
            d["space_state"] = "cold"
        else:
            d["space_state"] = space["severity"]
        d["total_h"] = human(total)
        d["used_h"] = human(used)
        d["free_h"] = human(free)
        d["scanned_ago"] = ago(d.get("last_scanned"))

        # The three bands the capacity bar draws: the library, the copies kept
        # beside it in redundancy folders, and everything MediaVault does not
        # track. A drive full of that last one has room to reclaim without
        # deleting a single title. Widths stay unrounded, or they drift.
        bands = media_by_drive.get(d["drive_id"], {})
        library = bands.get("library", 0)
        backups = bands.get("redundancy", 0)
        other = max(0, used - library - backups)
        d["library_h"] = human(library)
        d["backups_h"] = human(backups)
        d["other_h"] = human(other)
        d["bar"] = {
            "library": 100.0 * library / total,
            "backups": 100.0 * backups / total,
            "other": 100.0 * other / total,
        }
        d["library_pct"] = round(d["bar"]["library"], 1)
        d["backups_pct"] = round(d["bar"]["backups"], 1)
        d["other_pct"] = round(d["bar"]["other"], 1)
        # Only interesting once it is both large and a real share of the drive.
        d["other_notable"] = other > 20 * 2**30 and d["other_pct"] >= 15

        tags_by_path = db.get_tags_for_drive(d["drive_id"])
        hints = structure.hints_for_drive(d["drive_id"], hint_conn)
        tree = db.get_tree_for_drive(d["drive_id"])
        annotate_tree(tree, total, d["drive_id"], tags_by_path, dup_map, hints,
                      d["connected"], library_keys)
        d["tree"] = tree
        d["hint_count"] = len(hints)
        d["counts"] = summarise_counts(counts_by_drive.get(d["drive_id"]))

        largest = db.get_largest_folders(d["drive_id"], limit=6)
        for lf in largest:
            lf["size_h"] = human(lf["size_bytes"])
            lf["tags"] = tags_by_path.get(lf["rel_path"], [])
            lf["duplicates"] = dup_map.get(lf["id"], [])
            lf["backup_state"] = backup_state(lf["duplicates"], lf["tags"])
        d["largest_folders"] = largest

    hint_conn.close()

    grand_total = sum(d["total_bytes"] or 0 for d in drives)
    grand_used = sum(d["used_bytes"] or 0 for d in drives)
    grand_free = sum(d["free_bytes"] or 0 for d in drives)

    low_space_drives = [d for d in drives if d["low_space"]]
    same_drive_dups = db.same_drive_duplicates()
    flat_drives = db.flat_layout_drives()
    all_tags = db.get_all_distinct_tags()

    stats = {
        "connected_count": sum(1 for d in drives if d["connected"]),
        "drive_count": len(drives),
        "total_h": human(grand_total),
        "used_h": human(grand_used),
        "free_h": human(grand_free),
        "pct_used": round(100 * grand_used / grand_total, 1) if grand_total else 0,
        "hint_count": sum(d["hint_count"] for d in drives),
        # Nothing this process launches can draw a window in session 0, so the
        # page says so rather than letting Open look broken.
        "session_zero": fileops.in_session_zero(),
    }

    # Every category on every drive, folded together for the header. Backup
    # copies are counted apart, since a copy is not another title, and saying
    # so is what explains the gap against the filter bar's row count.
    combined = {}
    for per_category in counts_by_drive.values():
        for category, n in per_category.items():
            combined[category] = combined.get(category, 0) + n
    library = summarise_counts(combined)
    library["backups"] = sum(s["titles"] for s in db.redundancy_summaries().values())

    return render_template(
        "dashboard.html",
        drives=drives,
        stats=stats,
        library=library,
        low_space_drives=low_space_drives,
        same_drive_dups=same_drive_dups,
        flat_drives=flat_drives,
        same_drive_dup_h=human(sum(e["wasted_bytes"] for e in same_drive_dups)),
        all_tags=all_tags,
        system_tags=tagging.SYSTEM_TAGS,
        known_tags=tagging.KNOWN_TAGS,
        partial_ok_tag=tagging.PARTIAL_OK_TAG,
        star_tag=tagging.STAR_TAG,
        watching_tag=tagging.WATCHING_TAG,
        hint_meta=structure.HINTS,
        # Only the types that can be chosen, in the order the chips show them.
        drive_types=[(t, drivetypes.rule_for(t)["label"]) for t in drivetypes.ALL_TYPES],
        csrf_token=webguard.CSRF_TOKEN,
        csrf_header=webguard.CSRF_HEADER,
    )


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    results = db.search_nodes(q) if q else []
    # The live letter, so a result you can go and open right now is obvious.
    connected = scanner.get_connected_drives()
    for r in results:
        r["size_h"] = human(r["size_bytes"])
        mount = connected.get(r["drive_id"])
        r["connected"] = mount is not None
        r["drive_letter"] = drive_letter(mount, r.get("drive_letter_last"))
        # A title, or something inside one. Colour-coded in the result list so
        # a show is instantly distinguishable from one of its episodes.
        r["is_title"] = r["depth"] == 2
    return jsonify({"results": results})


@app.route("/api/promote", methods=["POST"])
def api_promote():
    """
    Turn an orphaned backup into a library title on its own drive.

    A rename within one drive, so it finishes at once and needs no job or
    progress panel, unlike a move between drives.
    """
    data = request.get_json(force=True)
    node_id = data.get("node_id")
    if not node_id:
        return jsonify({"ok": False, "error": "node_id is required"}), 400

    try:
        plan = moveops.plan_promote(int(node_id))
        if not data.get("confirm"):
            return jsonify({"ok": True, "planned": True, "plan": {
                "name": plan["name"], "category": plan["category"],
                "drive_label": plan["drive_label"],
                "size_h": human(plan["size_bytes"]),
                "from_rel": plan["source_rel"], "to_rel": plan["rel_path_after"],
            }})
        result = moveops.promote_title(plan)
    except moveops.MoveError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "promoted": result})


@app.route("/api/tag", methods=["POST"])
def api_tag():
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")
    rel_path = data.get("rel_path")
    tag = (data.get("tag") or "").strip()
    action = data.get("action", "add")

    if not drive_id or not rel_path or not tag:
        return jsonify({"ok": False, "error": "drive_id, rel_path, and tag are required"}), 400

    if action not in ("add", "remove"):
        return jsonify({"ok": False, "error": "action must be 'add' or 'remove'"}), 400

    # A tag describes the title, not the particular copy of it, so it follows
    # every copy. Starring something and finding its backup unstarred was
    # simply wrong: they are the same show.
    targets = [(drive_id, rel_path)] + db.copies_of(drive_id, rel_path)
    db.set_tag_bulk(targets, tag, action)

    return jsonify({
        "ok": True,
        "tag": tag,
        "action": action,
        # So the page can update the copies it is already showing.
        "applied_to": [{"drive_id": d, "rel_path": p} for d, p in targets],
        "copies": len(targets) - 1,
    })


@app.route("/api/tags")
def api_tags():
    return jsonify({"tags": db.get_all_distinct_tags()})


@app.route("/api/drive/label", methods=["POST"])
def api_drive_label():
    """Rename a drive. Everything is keyed on drive_id, so the label is
    cosmetic and safe to change even while the drive is unplugged."""
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

    # Same rule as the single-title endpoint: a tag follows every copy.
    spread = list(items)
    for drive_id, rel_path in items:
        spread.extend(db.copies_of(drive_id, rel_path))

    try:
        count = db.set_tag_bulk(spread, tag, action)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({
        "ok": True, "count": count, "tag": tag, "action": action,
        "applied_to": [{"drive_id": d, "rel_path": p} for d, p in spread],
    })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Start scanning every connected drive worth scanning. Returns at once
    with a job the page polls, since a full scan takes minutes."""
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


@app.route("/api/suggestions/sacrifice", methods=["POST"])
def api_sacrifice():
    """
    Delete one backup permanently, to free space on a drive that nothing can
    be moved off.

    Every condition is re-checked here rather than trusted from the page: the
    node must still be a backup, and an original must still exist somewhere
    else. The Recycle Bin is not offered, because a recycled file still
    occupies the space this exists to reclaim.
    """
    data = request.get_json(force=True)
    try:
        node_id = int(data.get("node_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "node_id must be an integer"}), 400
    if not data.get("confirm"):
        return jsonify({"ok": False, "error": "confirm is required"}), 400

    node = db.get_node(node_id)
    if node is None:
        return jsonify({"ok": False,
                        "error": "That title is no longer in the database. "
                                 "Rescan and try again."}), 400
    if node["depth"] != 2 or node["root_type"] != "redundancy":
        return jsonify({"ok": False,
                        "error": f"'{node['name']}' is not a backup, so it is "
                                 f"not something to delete for space."}), 400
    if not db.library_copies_of(node["name"], exclude_node_id=node_id):
        return jsonify({"ok": False,
                        "error": f"'{node['name']}' has no original left "
                                 f"anywhere, so this copy is the only one "
                                 f"there is. Promote it instead."}), 400

    try:
        result = fileops.delete_node(node_id, permanent=True)
    except fileops.FileOpError as e:
        return jsonify({"ok": False, "error": str(e)}), 409

    result["freed_h"] = human(result["bytes_freed"])
    return jsonify({"ok": True, "result": result})


@app.route("/api/node/delete", methods=["POST"])
def api_node_delete():
    """Delete the file or folder behind a node. mode is 'bin', recoverable
    but frees nothing until emptied, or 'permanent'."""
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


@app.route("/api/drive/type", methods=["POST"])
def api_drive_type():
    """
    Set a drive's type by hand, and whether it is cold storage.

    A type of null goes back to reading it off the volume label.
    """
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")

    drive = db.get_drive(drive_id) if drive_id else None
    if drive is None:
        return jsonify({"ok": False, "error": "no such drive"}), 404

    # Only touch what the request mentions. A missing key means leave it
    # alone, which is different from an explicit null meaning detect it.
    if "drive_type" in data:
        drive_type = data["drive_type"]
        if drive_type is not None and drive_type not in drivetypes.ALL_TYPES:
            return jsonify({
                "ok": False,
                "error": "drive_type must be null or one of: "
                         + ", ".join(drivetypes.ALL_TYPES),
            }), 400
        db.set_drive_type(drive_id, drive_type)
        # Changing to a card or a stick makes any existing cold storage flag
        # meaningless, so clear it rather than leaving it stored but ignored.
        if not drivetypes.allows_cold_storage(drive_type) and drive.get("cold_storage"):
            db.set_drive_cold_storage(drive_id, False)

    if "cold_storage" in data:
        effective = drivetypes.detect(
            label=drive["label"],
            stored=data.get("drive_type", drive.get("drive_type")),
        )
        if data["cold_storage"] and not drivetypes.allows_cold_storage(effective):
            return jsonify({
                "ok": False,
                "error": "Cards and USB sticks cannot be cold storage. Unpowered "
                         "flash has no stated retention and fails all at once, so "
                         "filling one and leaving it in a drawer is exactly what "
                         "loses the data.",
            }), 400
        db.set_drive_cold_storage(drive_id, bool(data["cold_storage"]))

    return jsonify({"ok": True})


def describe_target(d, mount, source_drive_id=None):
    """A drive as a move or backup target: how full, whether it is removable,
    and whether it already holds real backups."""
    drive_type = drivetypes.detect(label=d["label"], stored=d.get("drive_type"))
    space = drivetypes.evaluate(drive_type, d["free_bytes"], d["total_bytes"],
                                bool(d.get("cold_storage")))
    redundancy = db.redundancy_summary(d["drive_id"])
    # A drive that can be stored elsewhere is the only copy that survives
    # losing the machine, so it is worth pointing out.
    external = scanner.is_external(mount)

    return {
        "drive_id": d["drive_id"],
        "label": d["label"],
        "letter": drive_letter(mount, d.get("last_letter")),
        "free_bytes": d["free_bytes"],
        "free_h": human(d["free_bytes"]),
        "type_label": drivetypes.rule_for(drive_type)["label"],
        "cold_storage": bool(d.get("cold_storage")),
        "space": space,
        "external": external,
        "has_redundancy": redundancy["titles"] > 0,
        "redundancy_titles": redundancy["titles"],
        "redundancy_h": human(redundancy["size_bytes"]),
        "same_drive": d["drive_id"] == source_drive_id,
    }


@app.route("/api/suggestions")
def api_suggestions():
    """What is worth moving or backing up, and why. Proposals only: ticking
    one hands it to the existing move machinery."""
    include_recent = request.args.get("include_recent") == "1"
    try:
        recent_days = int(request.args.get("recent_days", suggestions.RECENT_DAYS))
    except ValueError:
        recent_days = suggestions.RECENT_DAYS

    result = suggestions.build(include_recent=include_recent, recent_days=recent_days)
    result["star_tag"] = tagging.STAR_TAG
    result["watching_tag"] = tagging.WATCHING_TAG

    # Sizes are formatted here so the page does not have to.
    for group in result["groups"]:
        group["would_free_h"] = human(group["would_free"])
        group["deficit_h"] = human(group["deficit_bytes"])
        for candidate in group["candidates"]:
            candidate["size_h"] = human(candidate["size_bytes"])
    for entry in result["redundancy"]["per_drive"]:
        entry["size_h"] = human(entry["size_bytes"])
    result["redundancy"]["total_h"] = human(result["redundancy"]["total_bytes"])

    return jsonify({"ok": True, **result})


def selected_nodes(arg="nodes"):
    """The titles a target list is being built for, skipping any that have
    since left the index."""
    ids = [i for i in request.args.get(arg, "").split(",") if i.strip().isdigit()]
    return [n for n in (db.get_node(int(i)) for i in ids) if n]


def annotate_conflicts(target, drive_id, nodes):
    """
    Which of these titles the drive already holds, as the original or as a
    backup. They are skipped rather than blocking the whole drive, so one
    clash in a selection of ten does not rule the drive out for the other
    nine. A drive that holds every one of them is dropped by the caller.
    """
    keys = db.title_keys_on_drive(drive_id)
    clashing = [n["name"] for n in nodes if moveops.existing_copy_on(drive_id, n, keys)]
    target["conflicts"] = len(clashing)
    target["conflict_names"] = clashing[:6]
    target["movable"] = len(nodes) - len(clashing)
    return target


@app.route("/api/drives/state")
def api_drive_state():
    """
    Which drives are plugged in right now.

    The page is rendered once, so without this a drive plugged in afterwards
    keeps its greyed out buttons until a reload. Deliberately tiny: the tree
    does not change, only whether the drive behind it can be read.
    """
    connected = scanner.get_connected_drives()
    return jsonify({"ok": True, "drives": {
        d["drive_id"]: {
            "connected": d["drive_id"] in connected,
            "letter": drive_letter(connected.get(d["drive_id"]), d.get("last_letter")),
        }
        for d in db.get_all_drives()
    }})


@app.route("/api/move/targets")
def api_move_targets():
    """
    Drives a selection could move to: connected, and not already holding the
    titles. A library title needs the target to have a library folder; a
    backup lands in its redundancy folder, which is created if missing.
    """
    exclude = request.args.get("exclude", "")
    nodes = selected_nodes()
    connected = scanner.get_connected_drives(max_age=0)
    needs_library = any(n["root_type"] != "redundancy" for n in nodes) if nodes else True

    targets = []
    for d in db.get_all_drives():
        if d["drive_id"] == exclude:
            continue
        mount = connected.get(d["drive_id"])
        if mount is None or scanner.is_cloud_drive(mount):
            continue
        if needs_library and moveops.find_library_root(mount) is None:
            continue
        target = annotate_conflicts(describe_target(d, mount, exclude), d["drive_id"], nodes)
        if nodes and not target["movable"]:
            continue
        targets.append(target)
    return jsonify({"ok": True, "targets": targets})


@app.route("/api/copy/targets")
def api_copy_targets():
    """
    Drives a backup could go to: any connected one that does not already hold
    a copy.

    The redundancy folder is created if missing, so a drive does not need one
    already. A drive holding the original, or a backup of its own, is left
    out: a second copy there dies with the first, which is the failure this
    is meant to survive.
    """
    connected = scanner.get_connected_drives()
    source = request.args.get("source", "")
    nodes = selected_nodes()

    targets = []
    for d in db.get_all_drives():
        mount = connected.get(d["drive_id"])
        if mount is None or d["drive_id"] == source or scanner.is_cloud_drive(mount):
            continue
        target = annotate_conflicts(describe_target(d, mount, source), d["drive_id"], nodes)
        if nodes and not target["movable"]:
            continue
        targets.append(target)
    return jsonify({"ok": True, "targets": targets})


@app.route("/api/move", methods=["POST"])
def api_move():
    """
    Move or back up titles.

    Takes either one destination, or a `batches` list of them which run one
    after another as a single job. That is what lets a set of suggestions
    spanning several drives be ticked and started in one go.
    """
    data = request.get_json(force=True)

    if "batches" in data:
        batches = data.get("batches") or []
    else:
        batches = [{"node_ids": data.get("node_ids") or [],
                    "target_drive_id": data.get("target_drive_id"),
                    "operation": data.get("operation", "move")}]

    try:
        for b in batches:
            b["node_ids"] = [int(i) for i in (b.get("node_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "node_ids must be integers"}), 400

    job, problem = movejob.start_batches(batches)
    if job is None:
        return jsonify({"ok": False, "error": problem}), 409
    return jsonify({"ok": True, "job": job})


@app.route("/api/move/status")
def api_move_status():
    return jsonify({"ok": True, "job": movejob.status()})


@app.route("/api/settings")
def api_settings():
    """Current settings, the copy programs found here, and which one will
    actually be used."""
    settings = config.load()
    tool, path, note = config.resolve_copy_tool(settings)
    return jsonify({
        "ok": True,
        "settings": settings,
        "tools": config.detect_copy_tools(),
        "effective_copy_tool": tool,
        "effective_path": path,
        "note": note,
        "config_path": config.CONFIG_PATH,
        "default_weights": config.DEFAULTS["backup_weights"],
        "db_path": db.db_path(),
        "db_path_warning": config.db_path_warning(db.db_path()),
        "db_env_override": bool(os.environ.get("MEDIAVAULT_DB")),
        # So the panel can say up front whether Backup can do anything,
        # rather than the button failing when pressed.
        "rclone_path": backup.rclone_path(),
    })


@app.route("/api/settings/weights/suggest")
def api_weights_suggest():
    """
    Weights worked out from the median size of a title in each bucket.

    A starting point rather than an answer: size stands in for what losing
    something costs, and it is wrong wherever a category happens to be small.
    """
    flat = db.flat_layout_drives()
    buckets = suggestions.weights_from_sizes()
    return jsonify({
        "ok": bool(buckets),
        "error": "" if buckets else "Nothing indexed yet. Scan a drive first.",
        "buckets": {k: {**v, "median_h": human(v["median_bytes"])}
                    for k, v in buckets.items()},
        "excluded_drives": [d["label"] for d in flat],
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Save settings from the dashboard."""
    data = request.get_json(force=True)
    settings = config.load()

    if "copy_tool" in data:
        settings["copy_tool"] = (data["copy_tool"] or "auto").lower()
    if "copy_tool_path" in data:
        path = (data["copy_tool_path"] or "").strip()
        if path and not os.path.isfile(path):
            return jsonify({"ok": False, "error": f"No file at:\n{path}"}), 400
        settings["copy_tool_path"] = path
        # Kept in step because older config files use this key.
        if settings["copy_tool"] == "teracopy" and path:
            settings["teracopy_path"] = path
    if "verify_after_copy" in data:
        settings["verify_after_copy"] = bool(data["verify_after_copy"])
    if "backup_target" in data:
        # Not validated here: that is a network round trip per save, and
        # rclone reports a bad target clearly enough when it runs.
        settings["backup_target"] = (data["backup_target"] or "").strip()
    if isinstance(data.get("backup_weights"), dict):
        weights = dict(settings["backup_weights"])
        for key, value in data["backup_weights"].items():
            if key not in weights:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                return jsonify({"ok": False,
                                "error": f"'{value}' is not a number."}), 400
            # Zero would let a whole category pile onto one drive unnoticed,
            # so the floor is a small positive weight rather than nothing.
            weights[key] = max(0.1, round(number, 2))
        settings["backup_weights"] = weights

    outcome = {"db_moved_to": None, "db_adopted": None, "db_replaced": None}
    if "db_path" in data:
        wanted = (data["db_path"] or "").strip()
        if os.environ.get("MEDIAVAULT_DB"):
            return jsonify({
                "ok": False,
                "error": "MEDIAVAULT_DB is set in the environment and wins over "
                         "this setting. Clear it first, or change it there.",
            }), 400

        target = db.resolve_db_path(wanted) if wanted else db.db_path()
        mode = data.get("db_path_mode")
        # Only when the path is actually changing: saving an unrelated setting
        # should not re-ask about a location already in use.
        if os.path.normcase(target) != os.path.normcase(db.db_path()):
            question = db_path_question(target, mode)
            if question:
                return jsonify({"ok": False, "confirm": question}), 409

        was = db.db_path()
        # This request has the database open, and Windows will not let a file
        # be removed while a handle is on it. Let it go before moving.
        _close_request_connection()
        try:
            if mode == "adopt":
                now = db.adopt_database(target)
            elif wanted:
                result = db.move_database(wanted, replace=(mode == "replace"))
                now, outcome["db_replaced"] = result["path"], result["replaced"]
            else:
                now = was
        except OSError as e:
            return jsonify({"ok": False, "error": str(e)}), 400

        # Stored only when it is somewhere other than beside the code, so
        # moving the code folder does not leave a stale absolute path winning
        # over the new default.
        settings["db_path"] = (
            "" if os.path.normcase(now) == os.path.normcase(db.DEFAULT_DB_PATH) else now)
        # Only report a change that happened. Saving other settings leaves the
        # path alone, and saying it moved would be a lie.
        if os.path.normcase(now) != os.path.normcase(was):
            outcome["db_adopted" if mode == "adopt" else "db_moved_to"] = now

    saved = config.save(settings)
    tool, path, note = config.resolve_copy_tool(saved)
    return jsonify({"ok": True, "settings": saved, "effective_copy_tool": tool,
                    "effective_path": path, "note": note,
                    "db_path": db.db_path(), **outcome})


@app.route("/api/backup", methods=["POST"])
def api_backup():
    """
    Snapshot the database and upload it. In the request rather than a
    background job: a couple of megabytes is a few seconds, and a job would
    need a dock entry and polling to say nothing but "done".
    """
    try:
        summary = backup.run()
    except backup.BackupError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **summary})


@app.route("/api/settings/browse", methods=["POST"])
def api_settings_browse():
    """
    Open a real Windows file picker and return what was chosen.

    A browser will not give a page a file's full path, so the server opens
    the dialog instead, which works only because it is the same machine. In
    its own process: a UI toolkit does not belong in a request thread.
    """
    if os.name != "nt":
        return jsonify({"ok": False, "error": "Only available on Windows."}), 400

    # folder=1 asks for a directory instead, which is what the database path
    # wants: a place to keep it, not a file to run.
    if request.args.get("folder") == "1":
        script = (
            "import tkinter as tk;"
            "from tkinter import filedialog;"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True);"
            "p = filedialog.askdirectory(title='Pick a folder for mediavault.db');"
            "print(p or '')"
        )
    else:
        script = (
            "import tkinter as tk;"
            "from tkinter import filedialog;"
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True);"
            "p = filedialog.askopenfilename("
            "title='Pick the program that should copy files',"
            "filetypes=[('Programs', '*.exe'), ('All files', '*.*')]);"
            "print(p or '')"
        )
    try:
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "The file picker was left open too long."}), 408
    except OSError as e:
        return jsonify({"ok": False, "error": f"Could not open a file picker: {e}"}), 500

    chosen = (result.stdout or "").strip()
    if not chosen:
        return jsonify({"ok": True, "path": None, "cancelled": True})
    return jsonify({"ok": True, "path": chosen, "cancelled": False})


@app.route("/api/nodes/delete", methods=["POST"])
def api_nodes_delete():
    """Delete several nodes, each attempted and reported on its own so one
    failure does not hide the rest."""
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
    """
    Connected drives that do not have the folder layout yet.

    Network drives are skipped unless ?network=1, because a share whose host
    is asleep takes ten seconds to give up and cannot be woken by being read
    anyway. The page loads the local drives immediately and offers to check
    the network ones on request.
    """
    include_network = request.args.get("network", "") in ("1", "true", "yes")

    # Order matters: probing is what discovers an unresponsive drive, so this
    # is only accurate once the candidates have been gathered.
    candidates = scanner.find_setup_candidates(include_network=include_network)
    stalled = scanner.unresponsive_roots()

    deferred = [] if include_network else scanner.deferred_network_roots()

    return jsonify({
        "ok": True,
        "candidates": candidates,
        "deferred": deferred,
        # Named rather than left out: a drive plainly visible in Explorer but
        # absent here reads as a bug.
        "unresponsive": [{"root": r, "letter": scanner.drive_letter_for(r) or r}
                         for r in stalled],
    })


@app.route("/api/drive/scaffold", methods=["POST"])
def api_drive_scaffold():
    """
    Create the folder layout on a drive already in the database, for the
    button inside an empty drive. Drives with no id yet go through
    /api/drives/setup, which works from a root path instead.
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
    Create the layout on the chosen drives, then scan them.

    Takes root paths rather than ids, since a drive with no library folder
    has never been scanned. Each root is checked against the drives actually
    connected, so a request cannot name an arbitrary path.
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
    """Forget a drive entirely. Only touches the database, nothing on the
    drive itself. Rescanning adds it back, but its tags are gone."""
    data = request.get_json(force=True)
    drive_id = data.get("drive_id")

    if not drive_id:
        return jsonify({"ok": False, "error": "drive_id is required"}), 400
    if not any(d["drive_id"] == drive_id for d in db.get_all_drives()):
        return jsonify({"ok": False, "error": "no such drive"}), 404

    db.delete_drive(drive_id)
    return jsonify({"ok": True})


HOST = os.environ.get("MEDIAVAULT_HOST", "127.0.0.1")

# --dev swaps waitress for Flask's own reloading server on a different port,
# so it runs beside the scheduled task rather than replacing it. The flag
# exists as well as the variable because setting an env var for one command
# is spelt differently in every shell, and getting it wrong silently starts
# the ordinary server instead.
DEV = ("--dev" in sys.argv
       or os.environ.get("MEDIAVAULT_DEV", "").strip().lower()
       in ("1", "true", "yes", "on"))
PORT = int(os.environ.get("MEDIAVAULT_PORT", "5152" if DEV else "5151"))

if __name__ == "__main__":
    webguard.check_bind(HOST)
    db.init_db()

    if DEV:
        print(f"MediaVault DEV on http://{HOST}:{PORT}  (auto-reloads on save)")
        print("The everyday copy is untouched - restart its scheduled task to "
              "pick these changes up for real.")
        # debug=True brings the reloader and in-browser tracebacks. It binds
        # localhost, which is what makes the debugger's console acceptable.
        app.run(host=HOST, port=PORT, debug=True)
        raise SystemExit

    # Waitress is meant to stay up for months, and gives a fixed thread pool
    # rather than one per connection, which matters while a scan runs in the
    # background. Fall back if it is not installed, so the app still starts.
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
