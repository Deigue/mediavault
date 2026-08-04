"""
watch_drives.py - WINDOWS ONLY. Auto-scan drives the moment they're plugged in.

This runs quietly in the background (e.g. started at login) and watches for
new removable/fixed drives being connected. When one appears, it waits a
few seconds for Windows to finish mounting it, then automatically runs the
same scan that scanner.py does - no manual step required at all.

SETUP - see the "Automating the watcher" section in README.md for full
step-by-step instructions, including how to run this silently at login
with the `py` launcher (pyw.exe). Quick version:

    py -m pip install pywin32
    py watch_drives.py          (to test it manually first)

NOTES
    - Only auto-labels new drives as "Unnamed (<letter>)" the first time;
      edit the label afterwards from the dashboard, or re-run
      scanner.py manually with --label to set a friendly name.
    - Skips the C: system drive by default (edit SKIP_DRIVES below if needed).
    - Ignores drives with no Movies/TV Shows/Anime/other folders (nothing to index).
"""

import string
import time
import os
import sys

import scanner
import db

SKIP_DRIVES = {"C:\\"}
POLL_SECONDS = 4
SETTLE_SECONDS = 3  # wait for Windows to finish mounting before scanning


def list_drives():
    if os.name != "nt":
        raise SystemExit("watch_drives.py only supports Windows.")
    import win32api
    drives = win32api.GetLogicalDriveStrings()
    return set(d for d in drives.split("\x00") if d)


def main():
    print("Watching for new drives... (Ctrl+C to stop)")
    db.init_db()
    known = list_drives()
    while True:
        time.sleep(POLL_SECONDS)
        try:
            current = list_drives()
        except Exception as e:
            print(f"Error listing drives: {e}")
            continue

        new_drives = current - known
        for drive in new_drives:
            if drive in SKIP_DRIVES:
                continue
            print(f"New drive detected: {drive} - waiting for it to settle...")
            time.sleep(SETTLE_SECONDS)
            try:
                sys.argv = ["scanner.py", drive]
                scanner.main()
            except SystemExit:
                pass
            except Exception as e:
                print(f"Failed to scan {drive}: {e}")

        known = current


if __name__ == "__main__":
    main()
