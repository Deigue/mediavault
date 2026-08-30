"""
moveops.py - moving and copying titles between drives.

A move is never a rename: copy, verify the copy arrived, then delete the
original. If anything fails the source is left where it was, so the worst
outcome is a half-written folder on the target that can be deleted and
retried. The copy itself is done by whichever tool config.py resolves to.
"""

import os
import re
import shutil
import subprocess
import threading
import time

import config
import db
import matching
import scanner

# robocopy uses exit codes 0-7 for success (files copied, extra files found,
# and so on). 8 and above mean at least one file failed to copy.
ROBOCOPY_OK_MAX = 7

# The dashboard runs under pythonw and has no console, so a console program
# started from it gets a new window that flashes on screen. The GUI copiers
# below are launched without this on purpose: their window is the point.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class MoveError(Exception):
    """Something went wrong that the user should be told about."""


def category_for(node):
    """The category a title sits in, e.g. "Anime". Titles are at depth 2, so
    this is their parent's name."""
    if node["parent_id"] is None:
        return None
    parent = db.get_node(node["parent_id"])
    return parent["name"] if parent else None


def is_backup(node):
    return node["root_type"] == "redundancy"


def existing_copy_on(target_drive_id, node, keys=None):
    """
    Where the target drive already holds this title, or None.

    Matched on the normalised name rather than the exact one, so a copy saved
    under a different release name still counts.
    """
    keys = keys if keys is not None else db.title_keys_on_drive(target_drive_id)
    key = matching.title_key(node["name"]) or node["name"].strip().lower()
    found = keys.get(key)
    return found[0] if found else None


def describe_clash(clash, node, target_label):
    """Why a target is refused, naming the copy that is in the way."""
    role = "a backup" if clash["root_type"] == "redundancy" else "a copy"
    return (f"{target_label} already holds {role} of '{node['name']}' at "
            f"{clash['rel_path']}. Two copies on one drive protect nothing.")


def plan_move(node_id, target_drive_id):
    """
    Where a title would go, without moving anything.

    A move never changes what the title is: a library title lands in the
    target's library, a backup lands in the target's redundancy folder. Use
    plan_promote to turn a backup into a library title.
    """
    node = db.get_node(node_id)
    if node is None:
        raise MoveError("That title is no longer in the database. Rescan and try again.")
    if node["depth"] != 2:
        raise MoveError("Only titles can be moved, not whole categories or single "
                        "files inside a title.")

    source_drive = db.get_drive(node["drive_id"])
    target_drive = db.get_drive(target_drive_id)
    if target_drive is None:
        raise MoveError("No such target drive.")
    if node["drive_id"] == target_drive_id:
        raise MoveError(f"'{node['name']}' is already on {target_drive['label']}.")

    source_mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if source_mount is None:
        raise MoveError(f"'{source_drive['label']}' is not connected.")
    target_mount = scanner.find_mounted_drive(target_drive_id, max_age=0)
    if target_mount is None:
        raise MoveError(f"'{target_drive['label']}' is not connected.")

    source_path = os.path.abspath(os.path.join(source_mount, node["rel_path"]))
    if not os.path.exists(source_path):
        raise MoveError(f"Not on disk any more:\n{source_path}")

    category = category_for(node)
    if not category:
        raise MoveError(f"Could not work out which category '{node['name']}' is in.")

    # Indexed copies are caught here, differently named ones included. The
    # on-disk check below still runs, for a copy made outside MediaVault.
    clash = existing_copy_on(target_drive_id, node)
    if clash:
        raise MoveError(describe_clash(clash, node, target_drive["label"]))

    backup = is_backup(node)
    if backup:
        root, created_root = find_redundancy_root(target_mount)
    else:
        root, created_root = find_library_root(target_mount), False
        if root is None:
            raise MoveError(f"'{target_drive['label']}' has no library folder. "
                            f"Set it up first.")

    target_dir = os.path.join(root, category)
    target_path = os.path.join(target_dir, node["name"])
    if os.path.exists(target_path):
        raise MoveError(f"'{node['name']}' already exists in {category} on "
                        f"{target_drive['label']}. Nothing was moved.")

    size = node["size_bytes"] or 0
    free = shutil.disk_usage(target_mount).free
    if size > free:
        raise MoveError(
            f"Not enough room on {target_drive['label']}: needs "
            f"{size / 2**30:.1f} GB, has {free / 2**30:.1f} GB free."
        )

    return {
        "node_id": node_id,
        "name": node["name"],
        "category": category,
        "size_bytes": size,
        "is_dir": bool(node["is_dir"]),
        "root_type": node["root_type"],
        "created_redundancy_root": created_root,
        "source_drive_id": node["drive_id"],
        "source_label": source_drive["label"],
        "source_path": source_path,
        "target_drive_id": target_drive_id,
        "target_label": target_drive["label"],
        "target_dir": target_dir,
        "target_path": target_path,
        "rel_path_after": os.path.relpath(target_path, target_mount),
        "target_mount": target_mount,
        "tags": db.get_tags_for_drive(node["drive_id"]).get(node["rel_path"], []),
    }


DEFAULT_REDUNDANCY_ROOT = "99_Redundancy"


def find_redundancy_root(mount, redundancy_prefix=None):
    """
    The drive's backup folder, or where one would go. Creates nothing.

    An existing folder wins whatever it is called, so a drive already using
    "Redundancy_Backup" keeps it rather than gaining a second one. Returns
    (path, would_create).
    """
    prefix = (redundancy_prefix or scanner.DEFAULT_REDUNDANCY_PREFIX).lower()
    existing = scanner.find_top_level_matches(mount, lambda n: prefix in n.lower())
    if existing:
        return existing[0], False
    return os.path.join(mount, DEFAULT_REDUNDANCY_ROOT), True


def find_or_make_redundancy_root(mount, redundancy_prefix=None):
    """The drive's backup folder, created if it has none. Only ever called
    once a plan has passed every check, so a refused plan leaves no litter."""
    path, creating = find_redundancy_root(mount, redundancy_prefix)
    if creating:
        os.makedirs(path, exist_ok=True)
    return path, creating


def plan_redundancy_copy(node_id, target_drive_id):
    """
    Where a backup copy would go, without copying. It lands in the target's
    redundancy folder under the same category, creating either if missing.
    """
    node = db.get_node(node_id)
    if node is None:
        raise MoveError("That title is no longer in the database. Rescan and try again.")
    if node["depth"] != 2:
        raise MoveError("Only whole titles can be backed up.")

    source_drive = db.get_drive(node["drive_id"])
    target_drive = db.get_drive(target_drive_id)
    if target_drive is None:
        raise MoveError("No such target drive.")

    source_mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if source_mount is None:
        raise MoveError(f"'{source_drive['label']}' is not connected.")
    target_mount = scanner.find_mounted_drive(target_drive_id, max_age=0)
    if target_mount is None:
        raise MoveError(f"'{target_drive['label']}' is not connected.")

    # A second copy on a drive that already has one is not redundancy: the
    # drive failing takes both, which is the case this exists to survive. The
    # copy in the way may be the original or another backup, and it may be on
    # the source drive or on the target.
    clash = existing_copy_on(target_drive_id, node)
    if clash:
        raise MoveError(describe_clash(clash, node, target_drive["label"]))

    source_path = os.path.abspath(os.path.join(source_mount, node["rel_path"]))
    if not os.path.exists(source_path):
        raise MoveError(f"Not on disk any more:\n{source_path}")

    category = category_for(node)
    if not category:
        raise MoveError(f"Could not work out which category '{node['name']}' is in.")

    redundancy_root, created_root = find_redundancy_root(target_mount)
    target_dir = os.path.join(redundancy_root, category)
    target_path = os.path.join(target_dir, node["name"])

    if os.path.exists(target_path):
        raise MoveError(f"A backup of '{node['name']}' already exists in "
                        f"{os.path.basename(redundancy_root)}/{category} on "
                        f"{target_drive['label']}.")

    size = node["size_bytes"] or 0
    free = shutil.disk_usage(target_mount).free
    if size > free:
        raise MoveError(
            f"Not enough room on {target_drive['label']}: needs "
            f"{size / 2**30:.1f} GB, has {free / 2**30:.1f} GB free."
        )

    return {
        "operation": "copy",
        "node_id": node_id,
        "name": node["name"],
        "category": category,
        "size_bytes": size,
        "is_dir": bool(node["is_dir"]),
        "source_drive_id": node["drive_id"],
        "source_label": source_drive["label"],
        "source_path": source_path,
        "target_drive_id": target_drive_id,
        "target_label": target_drive["label"],
        "target_dir": target_dir,
        "target_path": target_path,
        "target_mount": target_mount,
        "redundancy_root": os.path.basename(redundancy_root),
        "created_redundancy_root": created_root,
        "same_drive": node["drive_id"] == target_drive_id,
    }


def same_volume(a, b):
    """Are these two paths on one volume? Compared by device id, so a
    junction pointing at another disk is not mistaken for the same drive."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def plan_promote(node_id):
    """
    Where a backup would land if it became a library title, without moving
    anything.

    Promotion stays on its own drive, so it is a rename rather than a copy:
    nothing is read, nothing is verified, and no extra space is needed. It is
    for a backup whose original has gone, leaving the copy as the only one
    left and no longer a copy of anything.
    """
    node = db.get_node(node_id)
    if node is None:
        raise MoveError("That title is no longer in the database. Rescan and try again.")
    if node["depth"] != 2:
        raise MoveError("Only whole titles can be promoted.")
    if not is_backup(node):
        raise MoveError(f"'{node['name']}' is already a library title.")

    drive = db.get_drive(node["drive_id"])
    mount = scanner.find_mounted_drive(node["drive_id"], max_age=0)
    if mount is None:
        raise MoveError(f"'{drive['label']}' is not connected.")

    source_path = os.path.abspath(os.path.join(mount, node["rel_path"]))
    if not os.path.exists(source_path):
        raise MoveError(f"Not on disk any more:\n{source_path}")

    category = category_for(node)
    if not category:
        raise MoveError(f"Could not work out which category '{node['name']}' is in.")

    library_root = find_library_root(mount)
    if library_root is None:
        raise MoveError(f"'{drive['label']}' has no library folder. Set it up first.")

    # Promotion is a rename and nothing else. If the library root turned out
    # to sit on another volume, through a junction or a mount point,
    # shutil.move would quietly fall back to copy-then-delete with no
    # verification. Refused instead: this operation exists precisely because
    # it does not copy.
    if not same_volume(source_path, library_root):
        raise MoveError(
            f"'{drive['label']}' has its library folder on a different volume "
            f"from its backup folder, so promoting would copy rather than "
            f"rename. Move it by hand and rescan."
        )

    target_dir = os.path.join(library_root, category)
    target_path = os.path.join(target_dir, node["name"])
    if os.path.exists(target_path):
        raise MoveError(
            f"'{drive['label']}' already holds '{node['name']}' in its library. "
            f"Delete this backup instead, or move it to another drive: a second "
            f"copy on the same drive protects nothing."
        )

    # Promotion is only for an orphan. While an original survives, this is
    # still a backup doing its job, and promoting it would leave two library
    # copies and nothing marked as protection.
    originals = db.library_copies_of(node["name"], exclude_node_id=node_id)
    if originals:
        where = ", ".join(o["drive_label"] for o in originals[:3])
        raise MoveError(
            f"'{node['name']}' still has a library copy on {where}, so this is "
            f"a working backup rather than an orphan. Promote is only for a "
            f"backup whose original has gone."
        )

    return {
        "operation": "promote",
        "is_orphan": True,
        "node_id": node_id,
        "name": node["name"],
        "category": category,
        "size_bytes": node["size_bytes"] or 0,
        "is_dir": bool(node["is_dir"]),
        "drive_id": node["drive_id"],
        "drive_label": drive["label"],
        "source_path": source_path,
        "source_rel": node["rel_path"],
        "target_dir": target_dir,
        "target_path": target_path,
        "rel_path_after": os.path.relpath(target_path, mount),
        "tags": db.get_tags_for_drive(node["drive_id"]).get(node["rel_path"], []),
    }


def promote_title(plan, log=None):
    """Carry out a planned promotion. One rename inside a drive, so there is
    nothing to copy, verify or delete."""
    log = log or (lambda _m: None)
    log(f"  {plan['name']}")
    log(f"    {plan['drive_label']}: backup -> library / {plan['category']}")

    os.makedirs(plan["target_dir"], exist_ok=True)
    try:
        shutil.move(plan["source_path"], plan["target_path"])
    except OSError as e:
        raise MoveError(f"Could not promote '{plan['name']}': {e}")
    log(f"    now at {plan['rel_path_after']}")

    for tag in plan["tags"]:
        db.set_tag_bulk([(plan["drive_id"], plan["rel_path_after"])], tag, "add")
    db.delete_node_subtree(plan["node_id"])

    return {
        "name": plan["name"],
        "category": plan["category"],
        "size_bytes": plan["size_bytes"],
        "target_label": plan["drive_label"],
    }


def copy_title(plan, log=None, progress=None, on_file=None):
    """
    Back up a title. The source is never touched, and a copy that fails
    verification is left in place so it can be inspected.
    """
    log = log or (lambda _m: None)
    source, target = plan["source_path"], plan["target_path"]

    log(f"  {plan['name']}")
    log(f"    {plan['source_label']} -> {plan['target_label']} / "
        f"{plan['redundancy_root']} / {plan['category']}")
    if plan.get("created_redundancy_root"):
        log(f"    created {plan['redundancy_root']} on {plan['target_label']}")
    if plan.get("same_drive"):
        log("    note: same drive, so this protects against deleting it by "
            "accident but not against the drive failing")

    os.makedirs(plan["target_dir"], exist_ok=True)
    copy_tree(source, target, plan["is_dir"], log, progress, on_file)

    if config.load().get("verify_after_copy", True):
        verify_copy(source, target, log)
    else:
        log("    verification is turned off in config.json")

    return {
        "name": plan["name"],
        "category": plan["category"],
        "size_bytes": plan["size_bytes"],
        "target_label": plan["target_label"],
    }


def find_library_root(mount, root_prefix=None):
    """The drive's library folder, e.g. D:\\Videos, or None if it has none."""
    prefixes = [p.strip().lower()
                for p in (root_prefix or scanner.DEFAULT_ROOT_PREFIX).split(",")
                if p.strip()]
    roots = scanner.find_top_level_matches(
        mount, lambda n: any(n.lower().startswith(p) for p in prefixes)
    )
    return roots[0] if roots else None


# robocopy prints a percentage for the file it is on when /NP is left off.
# Anything else on the line is noise here.
ROBOCOPY_PCT = re.compile(r"^\s*([\d.]+)%\s*$")


def _copy_with_robocopy(source, target, is_dir, log, on_file=None):
    """
    robocopy only copies directories, so a single file is handled as 'this one
    file out of its parent folder'.

    Its output is read as it goes rather than collected at the end, which is
    what turns the folder-watching estimate into the name of the file actually
    being written and how far through it is.
    """
    common = ["/R:1", "/W:1", "/NDL", "/NJH", "/NJS"]
    if is_dir:
        args = ["robocopy", source, target, "/E"] + common
    else:
        args = ["robocopy", os.path.dirname(source), os.path.dirname(target),
                os.path.basename(source)] + common

    tail = []
    current = None
    try:
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError as e:
        raise MoveError(f"Could not start robocopy: {e}")

    with process:
        for line in process.stdout:
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            percent = ROBOCOPY_PCT.match(line)
            if percent:
                if on_file:
                    on_file(current, float(percent.group(1)))
                continue
            # A file line is "<size>\t<full path>"; anything else is a header
            # or a summary and is only kept in case the copy fails.
            parts = line.split("\t")
            if len(parts) >= 2 and parts[-1].strip():
                current = os.path.basename(parts[-1].strip())
                if on_file:
                    on_file(current, 0.0)
            tail.append(line)
            del tail[:-40]

    if process.returncode > ROBOCOPY_OK_MAX:
        raise MoveError(f"robocopy failed (exit {process.returncode}): "
                        + "\n".join(tail)[-400:])
    log(f"    robocopy finished (exit {process.returncode})")


# Command lines for the external copiers, as (args builder, friendly name).
def _external_args(tool, tool_path, source, target_parent):
    if tool == "fastcopy":
        # The trailing separator on /to= is required. Without it FastCopy
        # empties the source into the parent instead of creating the folder,
        # so a show's "Season 01" lands beside the other titles. join with an
        # empty tail adds it without doubling one already there.
        return [tool_path, "/cmd=diff", "/auto_close",
                f"/to={os.path.join(target_parent, '')}", source]
    # TeraCopy, and anything custom, take: <exe> Copy <source> <target dir>
    return [tool_path, "Copy", source, target_parent, "/Close"]


def _copy_with_external(tool, source, target, is_dir, tool_path, log, expected_bytes):
    """
    Hand the copy to an external program. These tend to pass the job to an
    already-running instance and exit at once, so the process finishing means
    nothing and the target is watched instead.
    """
    args = _external_args(tool, tool_path, source, os.path.dirname(target))
    log(f"    handing off to {os.path.basename(tool_path)}, its window shows the detail")
    try:
        subprocess.Popen(args).wait()
    except OSError as e:
        raise MoveError(f"Could not start {tool_path}: {e}")

    wait_until_settled(target, expected_bytes, log)
    log(f"    {os.path.basename(tool_path)} finished")


def wait_until_settled(target, expected_bytes, log, idle_timeout=120,
                       absent_timeout=30, poll=1.0):
    """
    Wait for a copy to stop changing. Returns at the expected size, or once
    it has not grown for idle_timeout; verify_copy decides if that is good
    enough.

    Nothing appearing at all is an error, not slowness. Every copier creates
    its destination within a second or two, so a target still missing after
    absent_timeout means the copy is landing somewhere else.
    """
    started = time.time()
    last_size, last_change = -1, time.time()
    while True:
        exists = os.path.exists(target)
        if not exists and time.time() - started > absent_timeout:
            raise MoveError(
                f"Nothing was written to {target}. The copy tool ran but put "
                f"its output somewhere else - check its arguments."
            )

        size = measure(target)[1] if exists else 0
        if expected_bytes and size >= expected_bytes:
            return
        if size != last_size:
            last_size, last_change = size, time.time()
        elif time.time() - last_change > idle_timeout:
            log(f"    target stopped growing at {size:,} bytes")
            return
        time.sleep(poll)


def _copy_with_python(source, target, is_dir, log):
    if is_dir:
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)
    log("    copied")


def copy_tree(source, target, is_dir, log, progress=None, on_file=None):
    """
    Copy source to target using the configured tool.

    progress is called with (files_done, total_files, bytes_done,
    total_bytes) about once a second. It measures the target as it fills
    rather than parsing tool output, so it works with any copier.

    on_file(name, percent) is the finer detail, and only robocopy can supply
    it. Everything else leaves it alone and the folder-watching figures stand
    on their own.
    """
    settings = config.load()
    tool, path, note = config.resolve_copy_tool(settings)
    if note:
        log("    " + note)

    total_files, total_bytes = measure(source)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    log(f"    copying {total_files} file(s), {total_bytes:,} bytes, with {tool}")

    stop = threading.Event()
    watcher = None
    if progress:
        progress(0, total_files, 0, total_bytes)

        def watch():
            while not stop.wait(1.0):
                try:
                    if os.path.exists(target):
                        files, done = measure(target)
                    else:
                        files, done = 0, 0
                    progress(files, total_files, done, total_bytes)
                except OSError:
                    continue

        watcher = threading.Thread(target=watch, name="mediavault-copy-progress", daemon=True)
        watcher.start()

    try:
        if tool == "robocopy":
            _copy_with_robocopy(source, target, is_dir, log, on_file)
        elif tool in ("teracopy", "fastcopy", "custom"):
            _copy_with_external(tool, source, target, is_dir, path, log, total_bytes)
        else:
            _copy_with_python(source, target, is_dir, log)
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=3)

    if progress:
        files, done = measure(target) if os.path.exists(target) else (0, 0)
        progress(files, total_files, done, total_bytes)


def measure(path):
    """(file count, total bytes) for a file or a whole folder."""
    if os.path.isfile(path):
        return 1, os.path.getsize(path)
    files, total = 0, 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda e: None):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                files += 1
            except OSError:
                continue
    return files, total


def verify_copy(source, target, log):
    """
    Confirm the copy arrived before anything is deleted. Compares file count
    and total size, which catches the failures that actually happen, a copy
    stopped partway or out of room, without hashing hundreds of gigabytes.
    """
    if not os.path.exists(target):
        raise MoveError("The copy did not arrive: nothing at the target path.")

    source_files, source_bytes = measure(source)
    target_files, target_bytes = measure(target)
    log(f"    source {source_files} files / {source_bytes:,} bytes")
    log(f"    target {target_files} files / {target_bytes:,} bytes")

    if target_files != source_files or target_bytes != source_bytes:
        raise MoveError(
            f"The copy does not match the source, so nothing was deleted.\n"
            f"Source: {source_files} files, {source_bytes:,} bytes.\n"
            f"Target: {target_files} files, {target_bytes:,} bytes.\n"
            f"The partial copy is still at {target}."
        )
    log("    verified")


def move_title(plan, log=None, progress=None, on_file=None):
    """Carry out a planned move: copy, verify, then delete the source. Tags
    follow the title to its new path."""
    log = log or (lambda _m: None)
    source, target = plan["source_path"], plan["target_path"]

    log(f"  {plan['name']}")
    where = (f"{os.path.basename(os.path.dirname(plan['target_dir']))} / "
             f"{plan['category']}" if plan.get("root_type") == "redundancy"
             else plan["category"])
    log(f"    {plan['source_label']} -> {plan['target_label']} / {where}")
    if plan.get("created_redundancy_root"):
        log(f"    created the backup folder on {plan['target_label']}")

    copy_tree(source, target, plan["is_dir"], log, progress, on_file)

    settings = config.load()
    if settings.get("verify_after_copy", True):
        verify_copy(source, target, log)
    else:
        log("    verification is turned off in config.json")

    # Only now is it safe to remove the original.
    try:
        if plan["is_dir"]:
            shutil.rmtree(source)
        else:
            os.remove(source)
    except OSError as e:
        raise MoveError(
            f"Copied successfully, but the original could not be deleted: {e}\n"
            f"The title now exists in both places. Remove the source by hand:\n"
            f"{source}"
        )
    log("    source removed")

    # Carry the tags across, then drop the old rows.
    destination = [(plan["target_drive_id"], plan["rel_path_after"])]
    for tag in plan["tags"]:
        db.set_tag_bulk(destination, tag, "add")
    db.delete_node_subtree(plan["node_id"])

    return {
        "name": plan["name"],
        "category": plan["category"],
        "size_bytes": plan["size_bytes"],
        "target_label": plan["target_label"],
    }
