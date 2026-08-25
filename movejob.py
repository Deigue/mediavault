"""
movejob.py - runs moves and backup copies in the background.

Copying a title can take a long time, so the dashboard starts a job here and
polls it, the same way scanning works. Only one job runs at a time.

Two operations share this machinery:

    move    to the target's library, then the source is deleted
    copy    to the target's redundancy folder, source left alone

Each title is planned again immediately before it runs. Space on the target
is checked per title, so a batch that runs out of room stops cleanly with the
remaining titles untouched rather than half copied.
"""

import threading
import time
import traceback

import db
import moveops
import scanner

MAX_LOG_LINES = 500

_lock = threading.Lock()
_current = None
_last_finished = None


def _new_job(plans, target_label, operation):
    return {
        "id": str(int(time.time() * 1000)),
        "operation": operation,          # 'move' or 'copy'
        "verb": "Moving" if operation == "move" else "Backing up",
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "target_label": target_label,
        "total": len(plans),
        "completed": 0,
        "current": None,
        # Progress within the title being worked on, so a single large title
        # does not look frozen while its episodes copy.
        "current_progress": None,
        "items": [
            {"name": p["name"], "category": p["category"],
             "size_bytes": p["size_bytes"], "status": "waiting", "detail": None}
            for p in plans
        ],
        "log": [],
        "moved": 0,
        "failed": 0,
    }


def _log(job, message):
    job["log"].append(message)
    if len(job["log"]) > MAX_LOG_LINES:
        del job["log"][:-MAX_LOG_LINES]


def _snapshot(job):
    if job is None:
        return None
    with _lock:
        copy = dict(job)
        copy["items"] = [dict(i) for i in job["items"]]
        copy["log"] = list(job["log"])
        copy["elapsed"] = (job["finished_at"] or time.time()) - job["started_at"]
        return copy


def _run(job, node_ids, target_drive_id, operation):
    global _current, _last_finished
    touched_drives = {target_drive_id}
    moving = operation == "move"
    try:
        for index, node_id in enumerate(node_ids):
            with _lock:
                job["items"][index]["status"] = "moving"
                job["current"] = job["items"][index]["name"]
                job["current_progress"] = None

            def on_progress(files_done, files_total, bytes_done, bytes_total, _i=index):
                with _lock:
                    job["current_progress"] = {
                        "files_done": files_done, "files_total": files_total,
                        "bytes_done": bytes_done, "bytes_total": bytes_total,
                        "pct": round(100 * bytes_done / bytes_total, 1) if bytes_total else 0,
                    }
                    job["items"][_i]["detail"] = (
                        f"{files_done}/{files_total} files" if files_total else "copying"
                    )

            try:
                # Re-plan now rather than trusting the plan made when the job
                # was queued: an earlier item in this batch has changed how
                # much room is left on the target.
                if moving:
                    plan = moveops.plan_move(node_id, target_drive_id)
                else:
                    plan = moveops.plan_redundancy_copy(node_id, target_drive_id)
                touched_drives.add(plan["source_drive_id"])

                runner = moveops.move_title if moving else moveops.copy_title
                result = runner(plan, log=lambda m: _log(job, m), progress=on_progress)
                with _lock:
                    job["items"][index].update(
                        status="done",
                        detail=("moved to " if moving else "backed up to ") + result["category"],
                    )
                    job["moved"] += 1
            except moveops.MoveError as e:
                with _lock:
                    job["items"][index].update(status="failed", detail=str(e).split("\n")[0])
                    job["failed"] += 1
                _log(job, f"  FAILED: {e}")
            except Exception as e:
                with _lock:
                    job["items"][index].update(status="failed", detail=str(e))
                    job["failed"] += 1
                _log(job, f"  FAILED: {e}")

            with _lock:
                job["completed"] = index + 1
                job["current_progress"] = None

        # Sizes and free space on both ends are now out of date.
        if job["moved"]:
            _log(job, "Rescanning affected drives...")
            for drive_id in touched_drives:
                mount = scanner.find_mounted_drive(drive_id, max_age=0)
                if mount:
                    try:
                        scanner.scan_and_store(mount, log=lambda _m: None)
                    except Exception as e:
                        _log(job, f"  rescan of {mount} failed: {e}")
            _log(job, "Done.")

        with _lock:
            job["status"] = "done"
    except Exception:
        with _lock:
            job["status"] = "failed"
        _log(job, "Move job crashed: " + traceback.format_exc(limit=3))
    finally:
        with _lock:
            job["finished_at"] = time.time()
            job["current"] = None
            _last_finished = job
            _current = None


def start(node_ids, target_drive_id, operation="move"):
    """
    Begin a move or a backup copy.

    Returns (job_snapshot, None) or (None, reason). Every title is planned up
    front so obvious problems are reported before anything is copied, rather
    than failing partway through.
    """
    global _current

    if operation not in ("move", "copy"):
        return None, "Unknown operation."

    with _lock:
        if _current is not None:
            return None, "Another move or copy is already running."

    if not node_ids:
        return None, "Nothing selected."

    target = db.get_drive(target_drive_id)
    if target is None:
        return None, "No such target drive."

    planner = moveops.plan_move if operation == "move" else moveops.plan_redundancy_copy
    plans, problems = [], []
    for node_id in node_ids:
        try:
            plans.append(planner(node_id, target_drive_id))
        except moveops.MoveError as e:
            problems.append(str(e))

    if not plans:
        return None, "Nothing can be done.\n\n" + "\n".join(problems)

    job = _new_job(plans, target["label"], operation)
    if problems:
        for p in problems:
            _log(job, "Skipped: " + p)

    with _lock:
        _current = job
    verb = "Moving" if operation == "move" else "Backing up"
    _log(job, f"{verb} {len(plans)} title(s) to {target['label']}.")

    thread = threading.Thread(target=_run,
                              args=(job, [p["node_id"] for p in plans],
                                    target_drive_id, operation),
                              name="mediavault-move", daemon=True)
    thread.start()
    return _snapshot(job), None


def status():
    return _snapshot(_current) or _snapshot(_last_finished)


def is_running():
    return _current is not None
