"""
scanjob.py - runs drive scans in the background for the dashboard.

A scan walks every folder on a drive and can take minutes, which is far too
long to hold an HTTP request open. So the dashboard starts a job here, gets
an id back straight away, and polls for progress while it runs.

Only one job runs at a time. The lock is what stops a double click, or two
browser tabs, from starting two scans that would fight over the same rows.
"""

import threading
import time
import traceback

import scanner

MAX_LOG_LINES = 400

_lock = threading.Lock()
_current = None          # the running job, or None
_last_finished = None     # kept so the UI can show the result after it ends


def _new_job(targets):
    return {
        "id": str(int(time.time() * 1000)),
        "status": "running",          # running | done | failed
        "started_at": time.time(),
        "finished_at": None,
        "total": len(targets),
        "completed": 0,
        "current": None,              # label or path of the drive being scanned
        "drives": [
            {"root": t["root"], "label": t.get("label") or t["root"],
             "status": "waiting", "detail": None}
            for t in targets
        ],
        "log": [],
        "error": None,
    }


def _log(job, message):
    job["log"].append(message)
    if len(job["log"]) > MAX_LOG_LINES:
        del job["log"][:-MAX_LOG_LINES]


def _snapshot(job):
    """A copy safe to hand to Flask while the worker thread keeps mutating."""
    if job is None:
        return None
    with _lock:
        copy = dict(job)
        copy["drives"] = [dict(d) for d in job["drives"]]
        copy["log"] = list(job["log"])
        copy["elapsed"] = (job["finished_at"] or time.time()) - job["started_at"]
        return copy


def _run(job, targets):
    global _current, _last_finished
    try:
        for index, target in enumerate(targets):
            with _lock:
                job["drives"][index]["status"] = "scanning"
                job["current"] = job["drives"][index]["label"]
            _log(job, f"Scanning {target['root']} ...")

            try:
                # Repeat whatever options this drive was last scanned with, so
                # a rescan cannot quietly drop folders from the index.
                options = (scanner.remembered_scan_options(target["drive_id"])
                           if target.get("drive_id") else {})
                summary = scanner.scan_and_store(
                    target["root"], log=lambda m: _log(job, "  " + m), **options
                )
                with _lock:
                    job["drives"][index].update(
                        status="done",
                        label=summary["label"],
                        detail=f"{summary['dir_count']} folders, {summary['file_count']} files",
                    )
            except Exception as e:
                with _lock:
                    job["drives"][index].update(status="failed", detail=str(e))
                _log(job, f"  FAILED: {e}")

            with _lock:
                job["completed"] = index + 1

        with _lock:
            job["status"] = "done"
    except Exception as e:
        with _lock:
            job["status"] = "failed"
            job["error"] = str(e)
        _log(job, "Scan job crashed: " + traceback.format_exc(limit=3))
    finally:
        with _lock:
            job["finished_at"] = time.time()
            job["current"] = None
            _last_finished = job
            _current = None


def start(targets=None):
    """
    Begin scanning. targets defaults to every connected drive worth scanning.

    Returns (job_snapshot, None) on success, or (None, reason) if a scan is
    already running or there is nothing to scan.
    """
    global _current

    with _lock:
        if _current is not None:
            return None, "A scan is already running."

    if targets is None:
        targets = scanner.find_scannable_drives()
    if not targets:
        return None, ("No drives to scan. Connect a drive that has a Videos "
                      "folder, or scan it once from the command line first.")

    job = _new_job(targets)
    with _lock:
        _current = job
    _log(job, f"Scanning {len(targets)} drive(s).")

    thread = threading.Thread(target=_run, args=(job, targets),
                              name="mediavault-scan", daemon=True)
    thread.start()
    return _snapshot(job), None


def status():
    """The running job, or the last one that finished, or None."""
    return _snapshot(_current) or _snapshot(_last_finished)


def is_running():
    return _current is not None
