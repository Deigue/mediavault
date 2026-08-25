"""
backup.py - copy the database somewhere safe, with rclone.

Backing up mediavault.db is not quite as simple as copying the file. db.py
runs the database in WAL mode, so recent commits live in a separate
mediavault.db-wal file until they are checkpointed. Anything that just reads
the .db - rclone, Explorer, a copy program - therefore captures an older
version of the data, and if a scan happens to be writing at that moment it
can capture a torn one.

So a snapshot is taken first. VACUUM INTO reads inside a transaction, folding
in whatever is sitting in the WAL and never seeing a half-written commit, and
writes out a single self-contained file. That file is what gets uploaded, and
it is why the backup goes through here rather than being one rclone command.

The destination is any rclone path, e.g. "gdrive:Backups/PC/mediavault". What
rclone is and how it reaches Google Drive is rclone's business, not ours -
this only needs it to be on PATH and configured.

    python backup.py                  back up to the configured target
    python backup.py gdrive:some/path back up somewhere else, just this once
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

import config
from db import DB_PATH

# Snapshots are written next to the database and replaced every run. Local
# scratch, nothing here needs to be kept or backed up itself.
WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backup")
SNAPSHOT_PATH = os.path.join(WORK_DIR, "mediavault.db")
LOG_FILE = os.path.join(WORK_DIR, "rclone.log")

# An upload of a few megabytes should never take this long. If it does,
# something is wrong - a dead network, or rclone sitting at a prompt - and
# the dashboard needs an answer rather than a hung request.
TIMEOUT_SECONDS = 180


class BackupError(Exception):
    """Something stopped the backup. The message is safe to show a user."""


def rclone_path():
    """Where rclone is, or None if it is not installed."""
    return shutil.which("rclone")


def snapshot(dest=SNAPSHOT_PATH):
    """Write a consistent, self-contained copy of the live database."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        os.remove(dest)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("VACUUM INTO ?", (dest,))
    finally:
        conn.close()
    return dest


def run(target=None):
    """
    Snapshot the database and upload it to target.

    Returns a summary dict on success. Raises BackupError with something
    worth reading if anything goes wrong, since both callers show the
    message straight to the user.
    """
    target = (target or config.load().get("backup_target") or "").strip()
    if not target:
        raise BackupError("No backup target set. Choose one in Settings.")
    if not rclone_path():
        raise BackupError(
            "rclone is not installed, or not on PATH. Install it from "
            "rclone.org and run 'rclone config' to connect your Drive."
        )
    if not os.path.exists(DB_PATH):
        raise BackupError("There is no database to back up yet.")

    try:
        snapshot()
    except sqlite3.Error as e:
        raise BackupError(f"Could not read the database: {e}")
    except OSError as e:
        raise BackupError(f"Could not write the snapshot: {e}")

    stamp = datetime.now().strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            [
                "rclone", "copy", SNAPSHOT_PATH, target,
                # Whatever is being replaced is moved aside instead of
                # discarded. Most of this database can be rebuilt by
                # rescanning, but tags are typed by hand and cannot be, so
                # today's upload must not be able to bury a better one.
                "--backup-dir", f"{target.rstrip('/')}/history/{stamp}",
                "--log-file", LOG_FILE,
                "--log-level", "NOTICE",
            ],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise BackupError("rclone took too long and was stopped.")
    except OSError as e:
        raise BackupError(f"Could not run rclone: {e}")

    if result.returncode != 0:
        # rclone puts the useful part last, and the whole log would be far
        # too much for a toast.
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {result.returncode}"
        raise BackupError(f"rclone failed: {tail}")

    return {
        "target": target,
        "bytes": os.path.getsize(SNAPSHOT_PATH),
        "at": datetime.now().isoformat(timespec="seconds"),
    }


if __name__ == "__main__":
    try:
        summary = run(sys.argv[1] if len(sys.argv) > 1 else None)
    except BackupError as e:
        raise SystemExit(str(e))
    print("Backed up %.1f MB to %s" % (summary["bytes"] / 1024.0 / 1024.0,
                                       summary["target"]))
