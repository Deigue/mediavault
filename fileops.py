"""
fileops.py - opening and deleting the actual files behind indexed nodes.

Everything here touches real files, so every path is resolved the same
careful way: look the node up in the database, find where its drive is
mounted right now, join the two, then confirm the result really does sit
inside that drive. Nothing acts on a path handed in by the caller.
"""

import os
import shutil
import subprocess

import db
import scanner


class FileOpError(Exception):
    """Something went wrong that the user should be told about."""


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


def open_in_shell(node_id):
    """
    Open a node the way double clicking it in Windows would.

    Folders open in whatever file manager is registered, files in whatever
    application handles that type. Returns the path that was opened.
    """
    abs_path, _node, _root = resolve_node_path(node_id)

    if os.name != "nt":
        raise FileOpError("Opening files is only supported on Windows.")

    try:
        os.startfile(abs_path)      # noqa: S606 - this is the whole point
    except OSError as e:
        raise FileOpError(f"Windows could not open it: {e}")
    return abs_path


def reveal_in_shell(node_id):
    """Open the containing folder with the item selected."""
    abs_path, _node, _root = resolve_node_path(node_id)

    if os.name != "nt":
        raise FileOpError("Opening files is only supported on Windows.")

    try:
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(abs_path)])
    except OSError as e:
        raise FileOpError(f"Could not open the folder: {e}")
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
