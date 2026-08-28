"""
fileops.py - opening and deleting the actual files behind indexed nodes.

Everything here touches real files, so every path is resolved the same
careful way: look the node up in the database, find where its drive is
mounted right now, join the two, then confirm the result really does sit
inside that drive. Nothing acts on a path handed in by the caller.
"""

import os
import shutil
import time

import db
import scanner


class FileOpError(Exception):
    """Something went wrong that the user should be told about."""


def in_session_zero():
    """
    Is this process running in Windows session 0, which has no desktop?

    A scheduled task set to "run whether user is logged on or not" lands
    there. The dashboard itself works, but anything it launches has nowhere
    to draw: opening a folder does nothing and playing a file gives audio
    from an invisible window. Worth saying rather than appearing broken.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        session = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(session))
        return bool(ok) and session.value == 0
    except Exception:
        return False


def resolve_node_path(node_id):
    """
    Turn a node id into a real path on disk.

    Returns (abs_path, node_row, mount_root). Raises FileOpError if the node
    is unknown, its drive is not plugged in, or the path escapes the drive.
    """
    node = db.get_node(node_id)
    if node is None:
        raise FileOpError("That item is no longer in the database. Rescan and try again.")

    drive = db.get_drive(node["drive_id"])
    label = drive["label"] if drive else node["drive_id"]

    mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if mount is None:
        raise FileOpError(f"'{label}' is not connected. Plug it in and try again.")

    abs_path = os.path.abspath(os.path.join(mount, node["rel_path"]))

    # The rel_path comes from our own scan, but check anyway: a stored path
    # containing ".." would otherwise reach outside the drive entirely.
    root = os.path.abspath(mount)
    if os.path.commonpath([root, abs_path]) != root:
        raise FileOpError("That path is outside the drive. Refusing to touch it.")

    if not os.path.exists(abs_path):
        raise FileOpError(f"Not found on disk any more:\n{abs_path}\n\n"
                          f"Rescan '{label}' to bring the index up to date.")

    return abs_path, node, root


ASFW_ANY = -1

# How long to watch for the window the launch produced. A file manager or a
# player is up well inside this; waiting longer would risk grabbing a window
# that has nothing to do with us.
FOREGROUND_WAIT_SECONDS = 2.0
FOREGROUND_POLL = 0.05


def _visible_windows():
    """Handles of every visible top-level window, as a set."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.add(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    return found


def _force_foreground(hwnd):
    """
    Bring a window to the front from a process that has no right to.

    Windows only lets the process that already owns the foreground hand it
    over. This one owns nothing: it is a background service and the browser
    has focus. Attaching to the foreground thread's input queue for the call
    is the documented way round that, and is what every launcher does.
    """
    import ctypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    SW_RESTORE = 9
    ours = kernel32.GetCurrentThreadId()
    front = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(front, None) if front else 0

    attached = bool(theirs) and bool(user32.AttachThreadInput(ours, theirs, True))
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.AllowSetForegroundWindow(ASFW_ANY)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)


def _raise_new_window(before):
    """Wait briefly for a window that was not there before, and front it."""
    deadline = time.monotonic() + FOREGROUND_WAIT_SECONDS
    while time.monotonic() < deadline:
        new = _visible_windows() - before
        if new:
            # Newest handle last is not guaranteed, but any of them is the
            # thing we just launched, and there is normally exactly one.
            _force_foreground(max(new))
            return True
        time.sleep(FOREGROUND_POLL)
    return False


def open_in_shell(node_id):
    """
    Open a node the way double clicking it in Windows would, in front.

    Folders open in whatever file manager is registered, files in whatever
    application handles that type. The launched window is brought forward
    explicitly: left alone it opens behind the browser and only blinks in the
    taskbar, which reads as nothing having happened.
    """
    abs_path, _node, _root = resolve_node_path(node_id)

    if os.name != "nt":
        raise FileOpError("Opening files is only supported on Windows.")

    try:
        before = _visible_windows()
    except Exception:
        before = None

    try:
        os.startfile(abs_path)      # noqa: S606 - this is the whole point
    except OSError as e:
        raise FileOpError(f"Windows could not open it: {e}")

    # Best effort. Failing to raise it is a worse experience, never an error.
    if before is not None:
        try:
            _raise_new_window(before)
        except Exception:
            pass
    return abs_path


def _send_to_recycle_bin(abs_path):
    try:
        from send2trash import send2trash
    except ImportError:
        raise FileOpError(
            "Sending to the Recycle Bin needs the send2trash package.\n"
            "Install it with: py -m pip install -r requirements.txt\n"
            "Or choose permanent delete instead."
        )
    try:
        send2trash(abs_path)
    except Exception as e:
        raise FileOpError(f"Could not send it to the Recycle Bin: {e}")


def _delete_permanently(abs_path):
    try:
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
    except OSError as e:
        raise FileOpError(
            f"Could not delete it: {e}\n\n"
            f"If something has the file open, close it and try again."
        )


def delete_node(node_id, permanent=False):
    """
    Delete the file or folder behind a node, then bring the index into line.

    permanent=False sends it to the Recycle Bin, which is recoverable but
    does not free space until the bin is emptied. permanent=True removes it
    outright and frees the space immediately.

    Returns a summary dict. The database is only updated once the delete has
    actually succeeded.
    """
    abs_path, node, root = resolve_node_path(node_id)

    was_dir = os.path.isdir(abs_path)
    if permanent:
        _delete_permanently(abs_path)
    else:
        _send_to_recycle_bin(abs_path)

    # Drop the node and everything under it, and correct the sizes above it.
    removed = db.delete_node_subtree(node_id)

    # Capacity changed, so refresh it while the drive is still to hand.
    try:
        total, used, free = shutil.disk_usage(root)
        db.update_drive_usage(node["drive_id"], total, used, free)
    except OSError:
        pass

    return {
        "path": abs_path,
        "name": node["name"],
        "was_dir": was_dir,
        "permanent": permanent,
        "nodes_removed": removed["nodes"],
        "tags_removed": removed["tags"],
        "bytes_freed": removed["size_bytes"],
    }
