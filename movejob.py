"""
movejob.py - runs moves and backup copies in the background.

Copying a title takes a long time, so the dashboard starts a job here and
polls it. One job at a time, but a job can span several destinations: ticking
suggestions that go to different drives queues them into a single run rather
than making you come back between each one.

Each title is planned again immediately before it runs, because an earlier
item in the same job has changed how much room is left on its target.

    move    to the target's library, then the source is deleted
    copy    to the target's redundancy folder, source left alone
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


def _verb_for(operation):
    return "Moving" if operation == "move" else "Backing up"


def _new_job(steps):
    """steps are the planned transfers, in the order they will run."""
    operations = {s["operation"] for s in steps}
    single = operations.pop() if len(operations) == 1 else None
    labels = list(dict.fromkeys(s["target_label"] for s in steps))

    return {
        "id": str(int(time.time() * 1000)),
        "operation": single or "mixed",
        "verb": _verb_for(single) if single else "Transferring",
        "status": "running",
        "started_at": time.time(),
        "finished_at": None,
        "target_label": labels[0] if len(labels) == 1 else f"{len(labels)} drives",
        # What the whole run covers, so the panel can show the queue up front.
        "targets": labels,
        "total": len(steps),
        "completed": 0,
        "current": None,
        # Progress within the title being worked on, so a single large title
        # does not look frozen while its episodes copy.
        "current_progress": None,
        # The single file being written, when the copier can say. robocopy
        # can; the others only give us the folder filling up.
        "current_file": None,
        "items": [
            {"name": s["name"], "category": s["category"],
             "size_bytes": s["size_bytes"], "operation": s["operation"],
             "target_label": s["target_label"], "status": "waiting", "detail": None}
            for s in steps
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


def _run(job, steps):
    global _current, _last_finished
    touched_drives = {s["target_drive_id"] for s in steps}
    try:
        for index, step in enumerate(steps):
            moving = step["operation"] == "move"
            with _lock:
                job["items"][index]["status"] = "moving"
                job["current"] = job["items"][index]["name"]
                job["current_file"] = None
                job["target_label"] = step["target_label"]
                job["verb"] = _verb_for(step["operation"])
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

            def on_file(name, percent):
                """The file robocopy is on right now, which is finer than the
                once-a-second folder measurement can be."""
                with _lock:
                    job["current_file"] = {"name": name, "pct": round(percent, 1)}

            try:
                planner = moveops.plan_move if moving else moveops.plan_redundancy_copy
                plan = planner(step["node_id"], step["target_drive_id"])
                touched_drives.add(plan["source_drive_id"])

                runner = moveops.move_title if moving else moveops.copy_title
                result = runner(plan, log=lambda m: _log(job, m),
                                progress=on_progress, on_file=on_file)
                with _lock:
                    job["items"][index].update(
                        status="done",
                        detail=("moved to " if moving else "backed up to ")
                               + f"{step['target_label']} / {result['category']}",
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
                job["current_file"] = None

        # Sizes and free space on every drive involved are now out of date.
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
    """One destination. Kept for the move and backup dialogs."""
    return start_batches([{"node_ids": node_ids,
                           "target_drive_id": target_drive_id,
                           "operation": operation}])


def start_batches(batches):
    """
    Begin a run covering one or more destinations, in the order given.

    Every title is planned up front so obvious problems are reported before
    anything is copied. A batch whose titles all fail is skipped and said so;
    the run only refuses outright if nothing at all can be done.

    Returns (job_snapshot, None) or (None, reason).
    """
    global _current

    with _lock:
        if _current is not None:
            return None, "Another move or copy is already running."

    if not batches:
        return None, "Nothing selected."

    steps, problems = [], []
    for batch in batches:
        operation = batch.get("operation", "move")
        if operation not in ("move", "copy"):
            return None, f"Unknown operation: {operation}"

        target_drive_id = batch.get("target_drive_id")
        target = db.get_drive(target_drive_id)
        if target is None:
            problems.append(f"No such target drive: {target_drive_id}")
            continue

        planner = moveops.plan_move if operation == "move" else moveops.plan_redundancy_copy
        for node_id in batch.get("node_ids") or []:
            try:
                plan = planner(node_id, target_drive_id)
            except moveops.MoveError as e:
                problems.append(str(e))
                continue
            steps.append({
                "node_id": node_id,
                "name": plan["name"],
                "category": plan["category"],
                "size_bytes": plan["size_bytes"],
                "operation": operation,
                "target_drive_id": target_drive_id,
                "target_label": target["label"],
            })

    if not steps:
        return None, "Nothing can be done.\n\n" + "\n".join(problems)

    job = _new_job(steps)
    for p in problems:
        _log(job, "Skipped: " + p)

    with _lock:
        _current = job

    if len(job["targets"]) == 1:
        _log(job, f"{job['verb']} {len(steps)} title(s) to {job['targets'][0]}.")
    else:
        _log(job, f"{len(steps)} title(s) across {len(job['targets'])} drives, "
                  f"one after another: {', '.join(job['targets'])}.")

    thread = threading.Thread(target=_run, args=(job, steps),
                              name="mediavault-move", daemon=True)
    thread.start()
    return _snapshot(job), None


def status():
    return _snapshot(_current) or _snapshot(_last_finished)


def is_running():
    return _current is not None
