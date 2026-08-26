"""
suggestions.py - works out what is worth moving, and where.

Two things drive a suggestion:

    relief    a drive is past the free space it should keep, so enough has to
              come off to get it back under
    reclaim   an SSD is holding bulk media. SSDs are the landing spot for
              downloads and anything that wants to be fast, so their space
              has standing value even when the drive is not in trouble

Both are only ever proposals. Nothing moves until it is ticked and confirmed.

The principles behind the ranking, in order:

Writes are the expensive operation. Reads cost an SSD nothing, and a
mechanical disk does not wear from writes in any way worth counting, so
moving from SSD to HDD is the cheapest direction there is. The reverse
spends SSD endurance to store films that never needed the speed, so it is
never suggested.

Freeing N bytes costs N bytes written whatever is chosen, so the only way to
spend less is to move less. Suggestions stop as soon as the drive is back
under its threshold.

Two tags decide what is off limits and what is worth protecting. A title
tagged as being watched is never moved, whatever its age, because a move
deletes the source once the copy lands and doing that to something an open
player is holding breaks playback. A starred title is one worth protecting,
and starred titles with no copy anywhere are what the backup suggestions
offer. Neither tag is ever applied automatically.

Recently arrived titles are also left alone, as a fallback for anything not
yet tagged.
"""

import time

import db
import drivetypes
import moveops
import scanner
import tagging

# A title that arrived within this many days is left alone as probably still
# in use. This is a fallback: the Watching tag says so directly and is always
# respected, so the window only has to cover the gap before something gets
# tagged, not a whole viewing season.
RECENT_DAYS = 21

# One drive holding more than this share of every backup is a single point of
# failure worth complaining about.
CONCENTRATION_WARN_PCT = 60

# Aim slightly past the threshold so one move does not leave the drive one
# episode away from warning again.
RELIEF_MARGIN_PCT = 2


def days_since(timestamp):
    if not timestamp:
        return None
    return (time.time() - timestamp) / 86400.0


def title_rows(drive_id):
    """Every title on a drive, with its timestamps."""
    conn = db.get_conn()
    rows = conn.execute(
        """
        SELECT id, name, rel_path, size_bytes, created_at, modified_at, root_type
        FROM nodes
        WHERE drive_id = ? AND depth = 2 AND root_type = 'library'
        ORDER BY size_bytes DESC
        """,
        (drive_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def age_of(title):
    """
    How long ago this title last gained content, in days.

    modified_at is the newest file anywhere inside, so a show still receiving
    episodes counts as recent even if the folder was created a year ago.
    Falls back to the folder's own creation time.
    """
    newest = title.get("modified_at") or title.get("created_at")
    return days_since(newest)


def describe_drives(connected):
    """Every known drive with the facts the ranking needs."""
    out = []
    for d in db.get_all_drives():
        mount = connected.get(d["drive_id"])
        drive_type = drivetypes.detect(label=d["label"], stored=d.get("drive_type"))
        cold = bool(d.get("cold_storage"))
        space = drivetypes.evaluate(drive_type, d["free_bytes"], d["total_bytes"], cold)
        out.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "mount": mount,
            "connected": mount is not None,
            "type": drive_type,
            "cold_storage": cold,
            "external": scanner.is_external(mount) if mount else False,
            "total_bytes": d["total_bytes"] or 0,
            "free_bytes": d["free_bytes"] or 0,
            "space": space,
            "has_library": bool(mount) and moveops.find_library_root(mount) is not None,
            "last_seen_path": d.get("last_seen_path"),
        })
    return out


def bytes_needed(drive):
    """How much has to come off to get this drive back under its threshold,
    plus a small margin so it does not warn again immediately."""
    total = drive["total_bytes"]
    if not total:
        return 0
    target_pct = drive["space"]["threshold_pct"] + RELIEF_MARGIN_PCT
    target_free = total * target_pct / 100.0
    return max(0, int(target_free - drive["free_bytes"]))


def rank_targets(source, drives, size_needed):
    """
    Where a title should go, best first.

    Mechanical disks come first: they do not wear from writes and this is
    exactly what they are for. An SSD is only ever offered if there is
    nothing else, since filling one with media is the thing this is trying to
    undo. Anything that would breach the target's own threshold is dropped
    rather than ranked low, because it would simply move the problem.
    """
    options = []
    for d in drives:
        if d["drive_id"] == source["drive_id"]:
            continue
        if not d["connected"] or not d["has_library"]:
            continue
        # A cold storage drive is declared read-only. Writing to it would
        # contradict the reason it is marked that way.
        if d["cold_storage"]:
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue

        # Would this land the target in trouble of its own?
        free_after = d["free_bytes"] - size_needed
        pct_after = 100.0 * free_after / (d["total_bytes"] or 1)
        if pct_after < d["space"]["threshold_pct"]:
            continue

        if d["type"] == drivetypes.SSD:
            rank = 3
            why = ("An SSD, so only offered because nothing else has room. "
                   "Storing media here is the thing this is trying to undo.")
        elif d["type"] == drivetypes.USB or d["external"]:
            rank = 2
            why = ("Removable, so fine for storage but not a permanent home. "
                   "It is not there when it is unplugged.")
        else:
            rank = 1
            why = ("A mechanical disk with room, which is what these are for. "
                   "Writes cost it nothing in wear.")

        options.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "free_bytes": d["free_bytes"],
            "free_after": free_after,
            "pct_after": round(pct_after, 1),
            "rank": rank,
            "why": why,
            "detail": f"{free_after / 2**30:.0f} GB free afterwards, {round(pct_after, 1)}%",
        })

    options.sort(key=lambda o: (o["rank"], -o["free_after"]))
    return options


def offline_options(drives, size_needed, for_backup=False):
    """
    Drives that would work but are not plugged in.

    Capacity is remembered from the last scan, so a drive that is unplugged
    is still known to have room. Worth saying so, because "connect the
    Toshiba" is often a better answer than squeezing something onto whatever
    happens to be attached.
    """
    out = []
    for d in drives:
        if d["connected"] or not d["total_bytes"]:
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue
        if not for_backup and d["cold_storage"]:
            continue

        held = db.redundancy_summary(d["drive_id"])["titles"]
        out.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "free_bytes": d["free_bytes"],
            "free_h": f"{d['free_bytes'] / 2**30:.0f} GB",
            "backups_held": held,
            "last_seen": d.get("last_seen_path"),
            "why": (f"Not connected, but had {d['free_bytes'] / 2**30:.0f} GB free "
                    f"when it was last scanned"
                    + (f" and holds {held} backup(s)." if held else ".")),
        })
    out.sort(key=lambda o: -o["free_bytes"])
    return out


def redundancy_distribution(drives=None):
    """
    How the backup copies are spread, and how much a single failure costs.

    Regular titles and backup copies want opposite things from a target. A
    title being moved should go wherever there is most room, so the big disk
    is the right answer. Backups should not: piling them all onto one big
    disk means that disk failing takes out most of the protection at once.
    Since backups are made deliberately, for the few titles worth protecting,
    that concentration is the worst outcome there is.

    So this measures the blast radius, the share of all backed up titles that
    live on any single drive, and it is what ranks targets for a backup.
    """
    drives = drives if drives is not None else describe_drives(
        scanner.get_connected_drives(max_age=0))

    per_drive, total_titles, total_bytes = [], 0, 0
    for d in drives:
        summary = db.redundancy_summary(d["drive_id"])
        total_titles += summary["titles"]
        total_bytes += summary["size_bytes"]
        per_drive.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "connected": d["connected"],
            "titles": summary["titles"],
            "size_bytes": summary["size_bytes"],
        })

    for entry in per_drive:
        entry["share_pct"] = (round(100.0 * entry["titles"] / total_titles, 1)
                              if total_titles else 0.0)

    worst = max(per_drive, key=lambda e: e["titles"], default=None)
    return {
        "per_drive": sorted(per_drive, key=lambda e: -e["titles"]),
        "total_titles": total_titles,
        "total_bytes": total_bytes,
        "drives_holding": sum(1 for e in per_drive if e["titles"] > 0),
        "worst_case": worst,
        "worst_case_pct": worst["share_pct"] if worst else 0.0,
    }


def rank_backup_targets(source_drive_id, size_needed, drives=None):
    """
    Where a backup copy should go, best first.

    The opposite of rank_targets. Most room is not the goal here; spreading
    is. A drive already holding few backups comes first, so each new copy
    lands where it adds least to any one drive's blast radius. A removable
    drive is preferred where it can be, since one kept unplugged survives
    things that take out the machine itself. The drive holding the original
    comes last and is marked, because a copy beside the original protects
    against deleting it by accident and nothing else.
    """
    drives = drives if drives is not None else describe_drives(
        scanner.get_connected_drives(max_age=0))
    counts = {e["drive_id"]: e for e in redundancy_distribution(drives)["per_drive"]}

    options = []
    for d in drives:
        if not d["connected"]:
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue

        held = counts.get(d["drive_id"], {}).get("titles", 0)
        same_drive = d["drive_id"] == source_drive_id

        # Cold storage is a reasonable home for a backup: it is written once
        # and then only read, which is exactly what a backup is.
        is_ssd = d["type"] == drivetypes.SSD
        if same_drive:
            why = ("Same drive as the original, so this guards against deleting "
                   "it by accident and nothing else. A drive failure takes both.")
        elif held == 0:
            why = ("Holds no backups yet, so a copy here spreads the risk rather "
                   "than adding to one pile.")
        else:
            why = (f"Already holds {held} backup(s). Fine, but a drive with fewer "
                   f"would spread the risk further.")
        if is_ssd and not same_drive:
            why += (" An SSD though, which a backup gains nothing from and which "
                    "has better uses for its space.")
        if d["external"] and not same_drive:
            why += " Removable, so it can be unplugged and kept elsewhere."

        options.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "cold_storage": d["cold_storage"],
            "backups_held": held,
            "free_bytes": d["free_bytes"],
            "same_drive": same_drive,
            "why": why,
            "detail": (f"{held} backup(s) here, {d['free_bytes'] / 2**30:.0f} GB free"),
            "rank": (
                1 if same_drive else 0,      # a copy beside the original is last
                1 if is_ssd else 0,          # an SSD is not where archives belong
                held,                        # then whoever holds fewest already
                0 if d["external"] else 1,   # removable preferred, can go offsite
                -d["free_bytes"],
            ),
        })

    options.sort(key=lambda o: o["rank"])
    for o in options:
        del o["rank"]
    return options


def starred_titles(drive_id):
    """Paths on a drive carrying the star, meaning worth protecting."""
    tags_by_path = db.get_tags_for_drive(drive_id)
    return {rel_path for rel_path, tags in tags_by_path.items()
            if tagging.has_tag(tags, tagging.STAR_TAG)}


def watching_titles(drive_id):
    """Paths tagged as being watched. Never proposed for a move: the source is
    deleted once the copy lands, and doing that to something in use breaks
    playback."""
    tags_by_path = db.get_tags_for_drive(drive_id)
    return {rel_path for rel_path, tags in tags_by_path.items()
            if tagging.has_tag(tags, tagging.WATCHING_TAG)}


def protect_groups(drives, by_id, backed_up, notes):
    """
    Titles marked as worth keeping that have no copy anywhere.

    Only tagged titles are proposed. Backups are made deliberately for the
    few things that matter, and guessing which those are would bury the
    handful that do under everything else.
    """
    groups = []
    tagged_anywhere = 0

    for source in drives:
        if not source["connected"]:
            continue
        starred = starred_titles(source["drive_id"])
        if not starred:
            continue
        tagged_anywhere += len(starred)

        candidates = []
        for title in title_rows(source["drive_id"]):
            if title["rel_path"] not in starred:
                continue
            if backed_up.get(title["id"]):
                continue          # already has a copy somewhere

            targets = rank_backup_targets(source["drive_id"], title["size_bytes"] or 0, drives)
            if not targets:
                continue

            size_gb = (title["size_bytes"] or 0) / 2**30
            candidates.append({
                "node_id": title["id"],
                "name": title["name"],
                "rel_path": title["rel_path"],
                "size_bytes": title["size_bytes"] or 0,
                "age_days": None,
                "backed_up": False,
                "why": (f"Starred, but exists in only one place. {size_gb:.0f} GB, "
                        f"and losing this drive loses it."),
                "targets": targets,
                "offline_targets": offline_options(drives, title["size_bytes"] or 0,
                                                   for_backup=True),
                "suggested_target": targets[0]["drive_id"],
            })

        if not candidates:
            continue

        groups.append({
            "drive_id": source["drive_id"],
            "label": source["label"],
            "type_label": drivetypes.rule_for(source["type"])["label"],
            "reason": "protect",
            "headline": (f"{len(candidates)} starred title(s) on {source['label']} "
                         f"have no copy anywhere."),
            "free_pct": source["space"]["free_pct"],
            "threshold_pct": source["space"]["threshold_pct"],
            "deficit_bytes": 0,
            "would_free": sum(c["size_bytes"] for c in candidates),
            "candidates": candidates,
        })

    if not tagged_anywhere:
        notes.append(
            "Nothing is starred, so no backups are suggested. Star the titles "
            "worth protecting and they appear here with somewhere sensible to "
            "put them."
        )
    return groups


def build(include_recent=False, recent_days=RECENT_DAYS):
    """
    The full set of proposals.

    Returns {'groups': [...], 'skipped_recent': n, 'notes': [...]}. Each group
    is one source drive with a reason and its candidate titles, each carrying
    its own ranked list of destinations.
    """
    connected = scanner.get_connected_drives(max_age=0)
    drives = describe_drives(connected)
    by_id = {d["drive_id"]: d for d in drives}
    backed_up = db.get_duplicate_map()

    groups = []
    skipped_recent = 0
    skipped_watching = 0
    notes = []

    for source in drives:
        if not source["connected"]:
            continue
        if source["cold_storage"]:
            continue

        deficit = bytes_needed(source)
        is_ssd = source["type"] == drivetypes.SSD

        if deficit > 0:
            reason = "relief"
            headline = (f"{source['label']} is at {source['space']['free_pct']}% free, "
                        f"below the {source['space']['threshold_pct']}% it should keep.")
        elif is_ssd:
            reason = "reclaim"
            headline = (f"{source['label']} is an SSD. Media sitting here is space "
                        f"that could be free for downloads and anything that wants "
                        f"to be fast.")
        else:
            continue

        titles = title_rows(source["drive_id"])
        if not titles:
            continue

        watching = watching_titles(source["drive_id"])

        candidates, freed = [], 0
        for title in titles:
            # Tagged as in use, so never move it. Unlike the age window this
            # is not a guess, and no checkbox overrides it.
            if title["rel_path"] in watching:
                skipped_watching += 1
                continue

            age = age_of(title)
            if age is not None and age < recent_days and not include_recent:
                skipped_recent += 1
                continue

            targets = rank_targets(source, drives, title["size_bytes"] or 0)
            if not targets:
                continue

            has_copy = bool(backed_up.get(title["id"]))
            size_gb = (title["size_bytes"] or 0) / 2**30
            if reason == "relief":
                why = (f"{size_gb:.0f} GB, one of the largest here, so moving it "
                       f"clears the most in a single go.")
            else:
                why = (f"{size_gb:.0f} GB of media on an SSD, which does not need "
                       f"the speed. Moving it to a mechanical disk is the cheapest "
                       f"direction there is: reads cost the SSD nothing and the "
                       f"disk does not wear from writes.")
            if age is not None:
                why += f" Last gained content {round(age)} days ago."
            if has_copy:
                why += " Already backed up elsewhere, so the move risks less."

            candidates.append({
                "node_id": title["id"],
                "name": title["name"],
                "rel_path": title["rel_path"],
                "size_bytes": title["size_bytes"] or 0,
                "age_days": round(age) if age is not None else None,
                "backed_up": has_copy,
                "why": why,
                "targets": targets,
                "offline_targets": offline_options(drives, title["size_bytes"] or 0),
                "suggested_target": targets[0]["drive_id"],
            })
            freed += title["size_bytes"] or 0

            # Relief only needs enough to clear the threshold. Moving more
            # than that is writes spent for nothing.
            if reason == "relief" and freed >= deficit:
                break

        if not candidates:
            if deficit > 0:
                waiting = offline_options(drives, deficit)
                if waiting:
                    names = ", ".join(f"{w['label']} ({w['free_h']} free)" for w in waiting[:3])
                    notes.append(f"{source['label']} needs {deficit / 2**30:.1f} GB moved off "
                                 f"and no connected drive has room. Connect {names}.")
                else:
                    notes.append(f"{source['label']} needs {deficit / 2**30:.1f} GB moved off, "
                                 f"but nothing suitable was found. Everything is either "
                                 f"recent, or there is no drive with room for it.")
            continue

        groups.append({
            "drive_id": source["drive_id"],
            "label": source["label"],
            "type_label": drivetypes.rule_for(source["type"])["label"],
            "reason": reason,
            "headline": headline,
            "free_pct": source["space"]["free_pct"],
            "threshold_pct": source["space"]["threshold_pct"],
            "deficit_bytes": deficit,
            "would_free": freed,
            "candidates": candidates,
        })

    # Relief first: those drives have an actual problem.
    groups.extend(protect_groups(drives, by_id, backed_up, notes))
    groups.sort(key=lambda g: ({"relief": 0, "protect": 1, "reclaim": 2}[g["reason"]],
                               -g["would_free"]))

    spread = redundancy_distribution(drives)
    if spread["total_titles"] and spread["worst_case_pct"] >= CONCENTRATION_WARN_PCT:
        notes.append(
            f"{spread['worst_case']['label']} holds {spread['worst_case_pct']}% of "
            f"every backup you have. If that drive fails you lose most of your "
            f"protection at once. Spread new backups across other drives."
        )

    return {"groups": groups, "skipped_recent": skipped_recent,
            "skipped_watching": skipped_watching, "notes": notes,
            "recent_days": recent_days, "redundancy": spread}
