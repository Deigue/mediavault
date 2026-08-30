"""
suggestions.py - what is worth moving or backing up, and where.

Five kinds, all only ever proposals, in the order they are shown:

    orphan    a backup whose original has gone, so it is the only copy left
    relief    a drive is past the free space it should keep
    protect   a starred title exists in only one place
    reclaim   an SSD is holding bulk media, and its space has standing value
    spread    one drive holds too much of all the protection there is

Relief can shed backups as well as library titles, since the original
survives the move. Where every move has been counted and the drive is still
short, it offers to delete backups instead, cheapest protection cost first
and never the only copy of anything. That is the one destructive proposal
here, and it sits inside the relief it serves rather than on its own.

Moves rank destinations by what a write costs. A mechanical disk does not
meaningfully wear from writes, so it comes first; an SSD is offered only when
nothing else has room. Cards and USB sticks are never offered, since a move
deletes the source and unpowered flash has no stated retention. A phone is
ordinary storage here: real controller, powered daily.

Backups rank the opposite way. Most room is not the goal, spreading is, so a
drive already holding few backups comes first and one failure cannot take out
most of the protection. Cards and sticks are an extra layer only and never
count as the copy that makes a starred title safe.

Two tags override everything. Watching is never moved, because a move deletes
the source and doing that to something a player has open breaks playback.
Star marks what the backup suggestions offer. Neither is ever applied for you.
"""

import time

import config
import db
import drivetypes
import matching
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

# A cold storage drive is kept full on purpose and is closed to writes. It
# reopens only once it has emptied well past its own threshold: a nearly full
# flash-backed volume has few spare sectors, and writing into that is what
# wears it. Hysteresis, so a drive that frees one title does not immediately
# start collecting them again.
COLD_REOPEN_USED_PCT = 70


def cold_and_closed(drive):
    """A cold storage drive still too full to accept writes."""
    if not drive["cold_storage"]:
        return False
    total = drive["total_bytes"] or 0
    if not total:
        return True
    used_pct = 100.0 * (total - drive["free_bytes"]) / total
    return used_pct >= COLD_REOPEN_USED_PCT


def backup_weights():
    """What losing one backed up title of each kind costs, relative to the
    others. Set in Settings; only the engine reads them."""
    return config.load()["backup_weights"]


def weights_from_sizes():
    """
    Weights seeded from your own library: the median size of a title in each
    bucket, scaled so the smallest bucket comes out at 1.

    The median, not the mean, because a handful of very large titles in one
    bucket would otherwise speak for all of it. Size is only a proxy for what
    losing something costs, and a poor one wherever a category happens to be
    small, so this is a starting point to adjust rather than an answer.

    Returns {bucket: {"median_bytes", "titles", "weight"}} for the buckets
    that have any titles at all.
    """
    # A drive laid out flat has episodes where titles should be, so letting
    # it into the medians would drag every bucket down.
    flat = [d["drive_id"] for d in db.flat_layout_drives()]
    out = {}
    for bucket, sizes in db.library_sizes_by_weight_bucket(flat).items():
        if not sizes:
            continue
        ordered = sorted(sizes)
        mid = len(ordered) // 2
        median = (ordered[mid] if len(ordered) % 2
                  else (ordered[mid - 1] + ordered[mid]) / 2)
        out[bucket] = {"median_bytes": median, "titles": len(sizes)}

    # "other" is whatever did not match a category, so on a drive laid out
    # flat it fills with episodes read as titles. Scaling against that would
    # let one badly arranged drive set the whole scale, so the baseline comes
    # from the classified buckets only.
    baseline = min((v["median_bytes"] for k, v in out.items()
                    if k != "other" and v["median_bytes"]), default=0)
    for entry in out.values():
        entry["weight"] = (round(entry["median_bytes"] / baseline, 1)
                           if baseline else 1.0)
    return out


def weight_of(category, weights=None):
    """The weight one title in this category carries."""
    weights = weights if weights is not None else backup_weights()
    return weights.get(tagging.weight_bucket_for_category(category), 1)


def days_since(timestamp):
    if not timestamp:
        return None
    return (time.time() - timestamp) / 86400.0


def title_rows(drive_id, include_backups=False):
    """
    Every title on a drive, with its timestamps and the category it sits in.

    Backups are left out unless asked for. Relief wants them, since shedding
    a copy costs nothing while the original survives, but nothing else does.
    """
    roots = ("library", "redundancy") if include_backups else ("library",)
    conn = db.get_conn()
    rows = conn.execute(
        f"""
        SELECT n.id, n.name, n.rel_path, n.size_bytes, n.created_at,
               n.modified_at, n.root_type, p.name AS category
        FROM nodes n JOIN nodes p ON n.parent_id = p.id
        WHERE n.drive_id = ? AND n.depth = 2
          AND n.root_type IN ({','.join('?' * len(roots))})
        ORDER BY n.size_bytes DESC
        """,
        (drive_id, *roots),
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


def _with_letter(drive_row, mount):
    letter = scanner.drive_letter_for(mount) or drive_row.get("last_letter")
    return f"{drive_row['label']} ({letter})" if letter else drive_row["label"]


def describe_drives(connected):
    """Every known drive with the facts the ranking needs."""
    out = []
    for d in db.get_all_drives():
        mount = connected.get(d["drive_id"])
        drive_type = drivetypes.detect(
            label=d["label"], stored=d.get("drive_type"),
            bus_type=scanner.get_bus_type(mount) if mount else None,
        )
        cold = bool(d.get("cold_storage"))
        space = drivetypes.evaluate(drive_type, d["free_bytes"], d["total_bytes"], cold)
        out.append({
            "drive_id": d["drive_id"],
            # The letter is shown beside the label, so it has to travel with
            # it: live where the drive is plugged in, remembered otherwise.
            "label": _with_letter(d, mount),
            "mount": mount,
            "connected": mount is not None,
            "type": drive_type,
            "cold_storage": cold,
            "external": scanner.is_external(mount) if mount else False,
            # A mapped share or an rclone mount. Reads go over the network, so
            # it is a worse home for something you play than any local disk.
            "remote": (scanner.get_drive_type(mount) == scanner.DRIVE_REMOTE
                       if mount else False),
            # A cloud client's virtual drive. Never a destination: the space
            # is a quota, not a disk, and a write queues an upload.
            "cloud": scanner.is_cloud_drive(mount) if mount else False,
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

    A move deletes the source, so the destination has to be somewhere the
    only copy can safely live. That rules out cards and sticks outright.
    Anything that would breach the target's own threshold is dropped rather
    than ranked low, since it would only move the problem.
    """
    options = []
    for d in drives:
        if d["drive_id"] == source["drive_id"]:
            continue
        if not d["connected"] or not d["has_library"]:
            continue
        # Its space is a quota on someone else's computer, and a write here
        # queues an upload rather than landing on a disk.
        if d["cloud"]:
            continue
        # Cold storage is declared read-only until it has emptied enough to
        # take writes without wearing itself out.
        if cold_and_closed(d):
            continue
        # Never the destination of a move. Unpowered flash has no stated
        # retention and fails all at once, and a move leaves nothing behind.
        if drivetypes.is_flash(d["type"]):
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue

        # Would this land the target in trouble of its own?
        free_after = d["free_bytes"] - size_needed
        pct_after = 100.0 * free_after / (d["total_bytes"] or 1)
        if pct_after < d["space"]["threshold_pct"]:
            continue

        # Closest first. A title that lives here is one you play, and a local
        # disk reads fastest and is the only one that cannot be unplugged, go
        # offline, or walk out of the building. Backups rank the other way,
        # in rank_backup_targets, where being elsewhere is the whole point.
        if d["type"] == drivetypes.SSD:
            rank = 5
            why = ("An SSD, so only offered because nothing else has room. "
                   "Storing media here is the thing this is trying to undo.")
        elif d["type"] == drivetypes.PHONE:
            rank = 4
            why = ("A phone, which is proper storage: a real controller, and "
                   "powered daily so nothing goes stale. It is mounted over "
                   "the network and it leaves the house, though, so the copy "
                   "goes with it.")
        elif d["remote"]:
            rank = 3
            why = ("A network drive. Fine for storage, but every read crosses "
                   "the network, so playback is slower and depends on the far "
                   "end being up.")
        elif d["external"]:
            rank = 2
            why = ("Removable, so fine for storage but not a permanent home. "
                   "It is not there when it is unplugged.")
        else:
            rank = 1
            why = ("A local mechanical disk with room, which is what these are "
                   "for. Fastest to play from, and writes cost it no wear.")

        options.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "free_bytes": d["free_bytes"],
            "total_bytes": d["total_bytes"],
            "threshold_pct": d["space"]["threshold_pct"],
            "free_after": free_after,
            "pct_after": round(pct_after, 1),
            "rank": rank,
            "why": why,
        })

    options.sort(key=lambda o: (o["rank"], -o["free_after"]))
    return options


def offline_options(drives, size_needed, for_backup=False, summaries=None):
    """
    Drives that would work but are not plugged in. Capacity is remembered
    from the last scan, so plugging one in is often a better answer than
    squeezing something onto whatever happens to be attached.
    """
    summaries = summaries if summaries is not None else db.redundancy_summaries()
    out = []
    for d in drives:
        if d["connected"] or not d["total_bytes"]:
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue
        # Same rules a connected drive would face: neither is a move target.
        if not for_backup and (d["cold_storage"] or drivetypes.is_flash(d["type"])):
            continue

        held = summaries.get(d["drive_id"], {}).get("titles", 0)
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


def redundancy_distribution(drives=None, summaries=None):
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
        scanner.get_connected_drives())
    summaries = summaries if summaries is not None else db.redundancy_summaries()

    weights = backup_weights()
    per_drive, total_titles, total_bytes, total_weight = [], 0, 0, 0.0
    for d in drives:
        summary = summaries.get(d["drive_id"], {"titles": 0, "size_bytes": 0})
        # What losing this drive would cost, in weighted titles rather than
        # bytes: thirty small shows hurt more than one large one.
        weight = sum(weights.get(bucket, 1) * n
                     for bucket, n in (summary.get("buckets") or {}).items())
        total_titles += summary["titles"]
        total_bytes += summary["size_bytes"]
        total_weight += weight
        per_drive.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "connected": d["connected"],
            "titles": summary["titles"],
            "size_bytes": summary["size_bytes"],
            "buckets": dict(summary.get("buckets") or {}),
            "weight": weight,
        })

    for entry in per_drive:
        entry["share_pct"] = (round(100.0 * entry["weight"] / total_weight, 1)
                              if total_weight else 0.0)
        # Kept alongside, so the panel can still say "10 of 11 titles".
        entry["title_share_pct"] = (round(100.0 * entry["titles"] / total_titles, 1)
                                    if total_titles else 0.0)

    worst = max(per_drive, key=lambda e: e["weight"], default=None)
    return {
        "per_drive": sorted(per_drive, key=lambda e: -e["weight"]),
        "total_titles": total_titles,
        "total_bytes": total_bytes,
        "total_weight": total_weight,
        "weights": weights,
        "drives_holding": sum(1 for e in per_drive if e["titles"] > 0),
        "worst_case": worst,
        "worst_case_pct": worst["share_pct"] if worst else 0.0,
    }


def rank_backup_targets(source_drive_id, size_needed, drives=None, counts=None):
    """
    Where a backup copy should go, best first.

    The opposite of rank_targets. Most room is not the goal here, spreading
    is, so a drive already holding few backups comes first and each new copy
    adds least to any one drive's blast radius. Removable is preferred, since
    a drive kept unplugged survives what takes out the machine.

    A card or a stick is an extra layer only, never protection, so it ranks
    below every real drive. A phone ranks as an ordinary drive: it is powered
    daily and refreshes itself, and it is genuinely offsite.
    """
    drives = drives if drives is not None else describe_drives(
        scanner.get_connected_drives())
    # Passed in by build(), which works it out once. Recomputing it per
    # candidate was the bulk of the time a build took.
    if counts is None:
        counts = {e["drive_id"]: e for e in redundancy_distribution(drives)["per_drive"]}

    options = []
    for d in drives:
        if not d["connected"]:
            continue
        # A copy on the source drive is not redundancy. The drive failing
        # takes both, which is the case this exists to survive.
        if d["drive_id"] == source_drive_id:
            continue
        if d["cloud"]:
            continue
        # Same rule as a move: a drive kept full on purpose is not somewhere
        # to put a copy until it has emptied enough to take one.
        if cold_and_closed(d):
            continue
        if d["free_bytes"] - size_needed <= 0:
            continue
        # Protecting one title by pushing another drive into trouble just
        # moves the problem. bytes_needed rather than the low flag, so a drive
        # sitting in the margin above its threshold, which relief is already
        # trying to shed from, is not handed more.
        if bytes_needed(d) > 0:
            continue
        free_after = d["free_bytes"] - size_needed
        if 100.0 * free_after / (d["total_bytes"] or 1) < d["space"]["threshold_pct"]:
            continue

        held = counts.get(d["drive_id"], {}).get("titles", 0)
        same_drive = False
        is_ssd = d["type"] == drivetypes.SSD
        is_flash = drivetypes.is_flash(d["type"])

        # The sentence about how much this drive holds is left to the client,
        # which knows what is ticked and so what the drive will actually be
        # holding. Everything that does not move is built here, once.
        if is_flash:
            why = (f"A {drivetypes.rule_for(d['type'])['label'].lower()}, so treat "
                   f"this as a spare copy rather than protection. Unpowered flash "
                   f"has no stated retention and tends to fail all at once.")
        elif held == 0:
            why = ("Holds no backups yet, so a copy here spreads the risk rather "
                   "than adding to one pile.")
        else:
            why = (f"Already holds {held} backup(s). Fine, but a drive with fewer "
                   f"would spread the risk further.")
        why_extras = ""

        if not same_drive and not is_flash:
            if is_ssd:
                why_extras += (" An SSD though, which a backup gains nothing from "
                               "and which has better uses for its space.")
            if d["type"] == drivetypes.PHONE:
                why_extras += (" A phone counts as a real copy, and it is the one "
                               "drive that is genuinely elsewhere. Losing or "
                               "resetting it loses the copy.")
            elif d["external"]:
                why_extras += " Removable, so it can be unplugged and kept elsewhere."
            why += why_extras

        options.append({
            "drive_id": d["drive_id"],
            "label": d["label"],
            "type_label": drivetypes.rule_for(d["type"])["label"],
            "external": d["external"],
            "cold_storage": d["cold_storage"],
            "backups_held": held,
            # Weighted, and sent because the client adds each ticked backup to
            # it and re-ranks: a drive that just gained a show is a worse home
            # for the next one.
            "weight_held": counts.get(d["drive_id"], {}).get("weight", 0),
            "free_bytes": d["free_bytes"],
            "same_drive": same_drive,
            # The UI marks these so a spare copy never reads as protection.
            "counts_as_protection": not is_flash,
            "why": why,
            # Everything after the "holds N" sentence, so the client can
            # rebuild that sentence against what is ticked without owning a
            # second copy of the rest.
            "why_extras": why_extras,
            "is_flash": is_flash,
            "backups_here": held,
            "total_bytes": d["total_bytes"],
            "threshold_pct": d["space"]["threshold_pct"],
            "free_after": d["free_bytes"] - size_needed,
            "rank": (
                1 if same_drive else 0,      # a copy beside the original is last
                1 if is_flash else 0,        # a card is a spare, not protection
                1 if is_ssd else 0,          # an SSD is not where archives belong
                held,                        # then whoever holds fewest already
                0 if d["external"] else 1,   # removable preferred, can go offsite
                -d["free_bytes"],
            ),
        })

    options.sort(key=lambda o: o["rank"])
    # The client re-ranks as items are ticked, so it needs the tier that does
    # not depend on how much space is left.
    for o in options:
        o["tier"] = list(o["rank"][:5])
        del o["rank"]
    return options


# Most important first. Orphans lead because promoting one is instant, costs
# nothing, and until it happens the title is invisible to protect. Relief
# comes next because a full drive is a problem right now.
REASON_ORDER = {"orphan": 0, "relief": 1, "protect": 2, "reclaim": 3, "spread": 4}


def orphan_groups(drives):
    """
    Backups whose original has gone, grouped by the drive they sit on.

    Nothing is copied to fix one: the folder moves out of the redundancy root
    into the library on the same drive. Until that happens the title is
    counted as a backup of something that no longer exists, and protect
    cannot see it at all.
    """
    library_keys = db.library_title_keys()
    groups = []

    for source in drives:
        if not source["connected"]:
            continue
        candidates = []
        for title in title_rows(source["drive_id"], include_backups=True):
            if title["root_type"] != "redundancy":
                continue
            key = matching.title_key(title["name"]) or title["name"].strip().lower()
            if key in library_keys:
                continue
            size_gb = (title["size_bytes"] or 0) / 2**30
            candidates.append({
                "node_id": title["id"],
                "name": title["name"],
                "drive_id": source["drive_id"],
                "rel_path": title["rel_path"],
                "size_bytes": title["size_bytes"] or 0,
                "age_days": None,
                "backed_up": False,
                "starred": False,
                "watching": False,
                "is_backup": True,
                "reasons": ["orphan"],
                "operation": "promote",
                "why": (f"{size_gb:.0f} GB filed as a backup, but no copy of it "
                        f"is left anywhere else, so it is not a copy of "
                        f"anything. Promoting it moves it into this drive's "
                        f"library. Nothing is copied and no space is needed."),
                "targets": [],
                "offline_targets": [],
                "suggested_target": source["drive_id"],
            })

        if candidates:
            groups.append({
                "drive_id": source["drive_id"],
                "label": source["label"],
                "type_label": drivetypes.rule_for(source["type"])["label"],
                "reason": "orphan",
                "headline": (f"{len(candidates)} backup(s) on {source['label']} have "
                             f"no original left anywhere. They are the only copy."),
                "free_pct": source["space"]["free_pct"],
                "threshold_pct": source["space"]["threshold_pct"],
                "deficit_bytes": 0,
                "would_free": 0,
                "candidates": candidates,
            })
    return groups


def spread_groups(drives, spread, counts, weights, summaries, existing):
    """
    Backups piled onto one drive, offered to whoever holds least.

    Only the drive carrying the most is worth thinning, and only while a move
    would actually lower its share. Anything relief has already proposed is
    left out, so the same move never appears twice under two headings.
    """
    worst = spread.get("worst_case")
    if not worst or not spread["total_weight"]:
        return []

    source = next((d for d in drives if d["drive_id"] == worst["drive_id"]), None)
    if source is None or not source["connected"]:
        return []

    # Perfectly even would be one share each. Well past that is worth saying
    # something about; near it is just noise.
    holders = max(1, sum(1 for e in spread["per_drive"] if e["titles"]))
    even_share = 100.0 / max(holders, 2)
    if worst["share_pct"] < max(CONCENTRATION_WARN_PCT, even_share * 1.5):
        return []

    already = {c["node_id"] for g in existing for c in g["candidates"]}
    candidates = []
    for title in title_rows(source["drive_id"], include_backups=True):
        if title["root_type"] != "redundancy" or title["id"] in already:
            continue
        size = title["size_bytes"] or 0
        targets = [t for t in rank_backup_targets(source["drive_id"], size, drives, counts)
                   if spread_gain(source["drive_id"], t["drive_id"],
                                  title["category"], counts, weights)]
        if not targets:
            continue
        w = weight_of(title["category"], weights)
        candidates.append({
            "node_id": title["id"],
            "name": title["name"],
            "drive_id": source["drive_id"],
            "rel_path": title["rel_path"],
            "size_bytes": size,
            "age_days": None,
            "backed_up": False,
            "starred": False,
            "watching": False,
            "is_backup": True,
            "reasons": ["spread"],
            "why": (f"{source['label']} holds {worst['share_pct']}% of all your "
                    f"protection. Moving this one elsewhere means a single "
                    f"failure takes less of it at once."),
            "weight": w,
            "targets": targets,
            "offline_targets": offline_options(drives, size, for_backup=True,
                                               summaries=summaries),
            "suggested_target": targets[0]["drive_id"],
        })

    if not candidates:
        return []

    # Heaviest first: a show is worth more than an anime film, so it is the
    # one worth getting off the pile before the lighter ones.
    candidates.sort(key=lambda c: (-c["weight"], -c["size_bytes"]))
    return [{
        "drive_id": source["drive_id"],
        "label": source["label"],
        "type_label": drivetypes.rule_for(source["type"])["label"],
        "reason": "spread",
        "headline": (f"{source['label']} holds {worst['share_pct']}% of every backup "
                     f"you have. Losing it would take most of your protection "
                     f"at once."),
        "free_pct": source["space"]["free_pct"],
        "threshold_pct": source["space"]["threshold_pct"],
        "deficit_bytes": 0,
        "would_free": sum(c["size_bytes"] for c in candidates),
        "candidates": candidates,
    }]


def sacrifice_candidates(source, shortfall, copies_by_key, starred,
                         drives, counts, weights):
    """
    Backups worth deleting when moving things off a drive cannot free enough.

    Last resort, and only ever reached because every non-destructive option
    has already been counted and fallen short. A backup that could still be
    moved somewhere that improves the spread is never offered: relieving the
    drive by moving it costs nothing, so deleting it would be strictly worse.
    Ordered by what it costs:
    copies that another backup already covers first, then ones no protect
    rule cared about, then the ones that genuinely undo a protect. A title
    whose only copy is this one is never offered, whatever else is true.
    """
    out = []
    for title in title_rows(source["drive_id"], include_backups=True):
        if title["root_type"] != "redundancy":
            continue

        key = matching.title_key(title["name"]) or title["name"].strip().lower()
        others = [c for c in copies_by_key.get(key, []) if c["id"] != title["id"]]
        if not any(c["root_type"] == "library" for c in others):
            continue        # an orphan: this is the only copy there is

        # Somewhere it could go that also thins out the pile. If one exists,
        # spread will offer that move, and offering to delete the same title
        # at the same time is a contradiction.
        if any(spread_gain(source["drive_id"], t["drive_id"],
                           title["category"], counts, weights)
               for t in rank_backup_targets(source["drive_id"],
                                            title["size_bytes"] or 0,
                                            drives, counts)):
            continue

        spare = [c for c in others if c["root_type"] == "redundancy"]
        is_starred = title["rel_path"] in starred
        if spare:
            tier, cost = 0, (f"Another backup of this is on "
                             f"{spare[0]['drive_label']}, so deleting this one "
                             f"costs no protection at all.")
        elif not is_starred:
            tier, cost = 1, ("The only backup, but the title is not starred, so "
                             "nothing was protecting it on purpose.")
        else:
            tier, cost = 2, ("The only backup of a starred title. Deleting it "
                             "leaves that title in one place, undoing a protect "
                             "to keep this drive alive.")

        size = title["size_bytes"] or 0
        original = next(c for c in others if c["root_type"] == "library")
        out.append({
            "node_id": title["id"],
            "name": title["name"],
            "drive_id": source["drive_id"],
            "rel_path": title["rel_path"],
            "size_bytes": size,
            "age_days": None,
            "backed_up": True,
            "starred": is_starred,
            "watching": False,
            "is_backup": True,
            "reasons": ["sacrifice"],
            "operation": "delete",
            "tier": tier,
            "why": (f"{size / 2**30:.0f} GB. The original is on "
                    f"{original['drive_label']} and is untouched. {cost}"),
            "targets": [],
            "offline_targets": [],
            "suggested_target": None,
        })

    # Cheapest protection cost first, largest within each tier, then only as
    # many as the shortfall actually needs. Deleting a starred title's last
    # backup when a redundant one would have done is the mistake to avoid.
    out.sort(key=lambda c: (c["tier"], -c["size_bytes"]))
    taken, freed = [], 0
    for candidate in out:
        if freed >= shortfall:
            break
        taken.append(candidate)
        freed += candidate["size_bytes"]
    return taken


def spread_gain(source_id, target_id, category, counts, weights):
    """
    The weight moving one backup would take off the source drive, or 0 when
    the move would not improve the spread at all.

    It only helps if the target carries less than the source. Otherwise the
    copy has just been poured onto the next pile, and moving a library title
    would have freed the same space without concentrating anything.
    """
    w = weight_of(category, weights)
    here = counts.get(source_id, {}).get("weight", 0)
    there = counts.get(target_id, {}).get("weight", 0)
    return w if there + w <= here else 0


def paths_with_tag(tags_by_path, wanted):
    """Paths carrying one tag, from a drive's tag map."""
    return {rel_path for rel_path, tags in tags_by_path.items()
            if tagging.has_tag(tags, wanted)}


def is_protected(copies, by_id):
    """
    Does this title have a copy somewhere that counts as protection?

    A copy on a card or a stick does not. It is worth having, but it has no
    stated retention unpowered and fails without warning, so a starred title
    whose only other copy is there still needs somewhere better.
    """
    for copy in copies or []:
        drive = by_id.get(copy["drive_id"])
        if drive is None or not drivetypes.is_flash(drive["type"]):
            return True
    return False


def protect_groups(drives, by_id, backed_up, notes, counts, summaries):
    """
    Starred titles with no copy anywhere that counts.

    Only tagged titles are proposed. Backups are made deliberately for the
    few things that matter, and guessing which those are would bury the
    handful that do under everything else.
    """
    groups = []
    tagged_anywhere = 0

    for source in drives:
        if not source["connected"]:
            continue
        tags_here = db.get_tags_for_drive(source["drive_id"])
        starred = paths_with_tag(tags_here, tagging.STAR_TAG)
        if not starred:
            continue
        tagged_anywhere += len(starred)
        watching = paths_with_tag(tags_here, tagging.WATCHING_TAG)

        candidates = []
        for title in title_rows(source["drive_id"]):
            if title["rel_path"] not in starred:
                continue
            copies = backed_up.get(title["id"])
            if is_protected(copies, by_id):
                continue

            targets = rank_backup_targets(source["drive_id"], title["size_bytes"] or 0,
                                          drives, counts)
            if not targets:
                continue

            size_gb = (title["size_bytes"] or 0) / 2**30
            why = (f"Starred, but exists in only one place. {size_gb:.0f} GB, "
                   f"and losing this drive loses it.")
            if copies:
                why = (f"Starred, and its only other copy is on a card or a "
                       f"stick, which is a spare rather than protection. "
                       f"{size_gb:.0f} GB.")
            candidates.append({
                "node_id": title["id"],
                "name": title["name"],
                "drive_id": source["drive_id"],
                "rel_path": title["rel_path"],
                "size_bytes": title["size_bytes"] or 0,
                "age_days": None,
                "backed_up": False,
                "starred": True,
                "watching": title["rel_path"] in watching,
                "is_backup": False,
                "reasons": ["protect"],
                "weight": weight_of(title["category"]),
                "why": why,
                "targets": targets,
                "offline_targets": offline_options(drives, title["size_bytes"] or 0,
                                                   for_backup=True,
                                                   summaries=summaries),
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
    connected = scanner.get_connected_drives()
    drives = describe_drives(connected)
    by_id = {d["drive_id"]: d for d in drives}
    backed_up = db.get_duplicate_map()

    # Worked out once and threaded through. Both are the same for every
    # candidate, and asking per candidate was most of the cost of a build.
    summaries = db.redundancy_summaries()
    copies_by_key = db.copies_by_title_key()
    spread = redundancy_distribution(drives, summaries)
    counts = {e["drive_id"]: e for e in spread["per_drive"]}
    weights = spread["weights"]

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

        # Relief can shed backups too: the original survives the move, so it
        # risks nothing. Reclaim is about media that does not need an SSD's
        # speed, which is a library question.
        titles = title_rows(source["drive_id"], include_backups=(reason == "relief"))
        if not titles:
            continue

        tags_here = db.get_tags_for_drive(source["drive_id"])
        watching = paths_with_tag(tags_here, tagging.WATCHING_TAG)
        starred = paths_with_tag(tags_here, tagging.STAR_TAG)

        candidates, freed = [], 0
        for title in titles:
            is_backup = title["root_type"] == "redundancy"

            # Both filters protect something you are using or just added.
            # Neither means anything for a backup: moving one disturbs
            # nothing, since the original stays put and stays playable.
            if not is_backup:
                if title["rel_path"] in watching:
                    skipped_watching += 1
                    continue
                age = age_of(title)
                if age is not None and age < recent_days and not include_recent:
                    skipped_recent += 1
                    continue
            else:
                age = None

            size = title["size_bytes"] or 0
            if is_backup:
                targets = rank_backup_targets(source["drive_id"], size, drives, counts)
            else:
                targets = rank_targets(source, drives, size)
            if not targets:
                continue

            has_copy = bool(backed_up.get(title["id"]))
            size_gb = size / 2**30
            reasons = [reason]

            if is_backup:
                gain = spread_gain(source["drive_id"], targets[0]["drive_id"],
                                   title["category"], counts, weights)
                why = (f"{size_gb:.0f} GB, and a backup rather than the only copy, "
                       f"so moving it risks nothing.")
                if gain:
                    reasons.append("spread")
                    after = counts.get(source["drive_id"], {}).get("weight", 0) - gain
                    share_after = (round(100.0 * after / spread["total_weight"], 1)
                                   if spread["total_weight"] else 0.0)
                    why += (f" It also thins out {source['label']}, which holds "
                            f"{counts.get(source['drive_id'], {}).get('share_pct', 0)}% "
                            f"of all your protection, down to about {share_after}%.")
                else:
                    why += (" Every drive with room already holds as much backup "
                            "as this one, so it frees space without improving how "
                            "the copies are spread.")
            elif reason == "relief":
                why = (f"{size_gb:.0f} GB, one of the largest here, so moving it "
                       f"clears the most in a single go.")
            else:
                why = (f"{size_gb:.0f} GB of media on an SSD, which does not need "
                       f"the speed. Moving it to a mechanical disk is the cheapest "
                       f"direction there is: reads cost the SSD nothing and the "
                       f"disk does not wear from writes.")
            if age is not None:
                why += f" Last gained content {round(age)} days ago."
            if has_copy and not is_backup:
                why += " Already backed up elsewhere, so the move risks less."

            candidates.append({
                "node_id": title["id"],
                "name": title["name"],
                "drive_id": source["drive_id"],
                "rel_path": title["rel_path"],
                "size_bytes": size,
                "age_days": round(age) if age is not None else None,
                "backed_up": has_copy,
                "starred": title["rel_path"] in starred,
                "watching": False,
                "is_backup": is_backup,
                "reasons": reasons,
                "weight": weight_of(title["category"], weights) if is_backup else 0,
                "why": why,
                "targets": targets,
                "offline_targets": offline_options(drives, size,
                                                   for_backup=is_backup,
                                                   summaries=summaries),
                "suggested_target": targets[0]["drive_id"],
            })
            freed += size

            # Relief only needs enough to clear the threshold. Moving more
            # than that is writes spent for nothing.
            if reason == "relief" and freed >= deficit:
                break

        # Deleting a backup is the last resort, so it is only reached once
        # every move that could have helped has been counted and the drive is
        # still short. Anything that can be moved is moved instead.
        shortfall = max(0, deficit - freed) if reason == "relief" else 0
        sacrifice = (sacrifice_candidates(source, shortfall, copies_by_key,
                                          starred, drives, counts, weights)
                     if shortfall else [])

        if not candidates:
            if deficit > 0:
                waiting = offline_options(drives, deficit, summaries=summaries)
                if waiting:
                    names = ", ".join(f"{w['label']} ({w['free_h']} free)" for w in waiting[:3])
                    notes.append(f"{source['label']} needs {deficit / 2**30:.1f} GB moved off "
                                 f"and no connected drive has room. Connect {names}.")
                else:
                    notes.append(f"{source['label']} needs {deficit / 2**30:.1f} GB moved off, "
                                 f"but nothing suitable was found. Everything is either "
                                 f"recent, or there is no drive with room for it.")
            continue

        # Multi-purpose first, since one move buying two things is strictly
        # better value than one buying one, then largest.
        candidates.sort(key=lambda c: (-len(c["reasons"]), -c["size_bytes"]))

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
            "sacrifice": sacrifice,
            "shortfall_bytes": shortfall,
        })

    groups.extend(protect_groups(drives, by_id, backed_up, notes, counts, summaries))
    groups.extend(orphan_groups(drives))
    groups.extend(spread_groups(drives, spread, counts, weights, summaries, groups))
    groups.sort(key=lambda g: (REASON_ORDER.get(g["reason"], 99), -g["would_free"]))

    if spread["total_titles"] and spread["worst_case_pct"] >= CONCENTRATION_WARN_PCT:
        notes.append(
            f"{spread['worst_case']['label']} holds {spread['worst_case_pct']}% of "
            f"every backup you have. If that drive fails you lose most of your "
            f"protection at once. Spread new backups across other drives."
        )

    return {"groups": groups, "skipped_recent": skipped_recent,
            "skipped_watching": skipped_watching, "notes": notes,
            "recent_days": recent_days, "redundancy": spread}
