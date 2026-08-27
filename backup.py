"""
backup.py - copy the database somewhere safe, with rclone.

    py backup.py                  back up to the configured target
    py backup.py gdrive:some/path back up somewhere else, just this once

Not a plain file copy. The database runs in WAL mode, so recent commits sit
in a separate -wal file and anything reading the .db directly captures stale
or torn data. VACUUM INTO writes a self-contained snapshot inside a
transaction, and that is what gets uploaded.

The destination is any configured rclone path. rclone only needs to be on
PATH.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

import config
from db import DB_PATH

# Local scratch, replaced every run. Nothing here needs keeping.
WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".backup")
SNAPSHOT_PATH = os.path.join(WORK_DIR, "mediavault.db")
LOG_FILE = os.path.join(WORK_DIR, "rclone.log")

# A few megabytes should never take this long. Longer means a dead network or
# rclone sitting at a prompt, and the dashboard needs an answer either way.
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
    """Snapshot the database and upload it. Raises BackupError with a message
    both callers show straight to the user."""
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
                # Move the previous upload aside rather than discarding it.
                # Most of this database can be rebuilt by rescanning, but tags
                # are typed by hand, so today must not bury a better copy.
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
        # rclone puts the useful part last, and the log is too much for a toast.
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
