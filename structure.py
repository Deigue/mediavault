"""
structure.py - spot titles whose folder layout is out of date.

The convention is one folder per title, so an anime's seasons are top-level
folders rather than subfolders inside one. A folder that aggregates several
works breaks everything keyed on the title: backup matching, largest folders,
tags.

Hints only, never actions. Each says what looks wrong so it can be fixed by
hand.
"""

import os
import re

# "Season 2", "S01", "S01-05", "Part 3", "Cour 2". The leading separator is
# what stops a title beginning with S matching on its own first letter.
SEASON_RE = re.compile(
    r"(?:^|[\s._\-\[(])"
    r"(?:season|series|cour|part|s)\s*[._\-]?\s*(\d{1,2})"
    r"(?:\s*[-~]\s*(?:s)?\d{1,2})?"
    r"(?:$|[\s._\-\])])",
    re.IGNORECASE,
)

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}

# A title with more loose videos than this is a season's worth of episodes,
# not a stray film. Two covers a movie plus its OVA.
MAX_STRAY_VIDEOS = 2

# Below this a folder is a title with extras, not a collection of works.
MIN_COLLECTION_FOLDERS = 3

HINTS = {
    "seasons-inside": {
        "label": "Seasons buried inside",
        "detail": "The seasons are folders inside this one. The convention is a "
                  "top-level folder per season, so each is its own title.",
    },
    "collection": {
        "label": "Several titles in one folder",
        "detail": "This groups separate works rather than being one title. "
                  "Backup matching and tags all work per title, so anything "
                  "in here is invisible to them.",
    },
    "movie-inside": {
        "label": "Loose film in a series folder",
        "detail": "A film is sitting beside the season folders. Films belong "
                  "in the Movies or Anime Movies category.",
    },
}


def looks_like_season(name):
    return bool(SEASON_RE.search(name))


def is_video(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS


def hints_for(children, is_anime):
    """
    Which hints a title earns, given its direct children as (name, is_dir).

    seasons-inside is anime only. A TV show keeping its seasons in subfolders
    is the documented layout, so flagging those would be wrong.
    """
    season_dirs, other_dirs, videos = [], [], []
    for name, is_dir in children:
        if is_dir:
            (season_dirs if looks_like_season(name) else other_dirs).append(name)
        elif is_video(name):
            videos.append(name)

    found = []
    if is_anime and len(season_dirs) >= 2:
        found.append("seasons-inside")
    if not season_dirs and len(other_dirs) >= MIN_COLLECTION_FOLDERS:
        found.append("collection")
    if season_dirs and 1 <= len(videos) <= MAX_STRAY_VIDEOS:
        found.append("movie-inside")
    return found


def is_anime_category(rel_path):
    """Anime, but not Anime Movies, where one folder per film is correct."""
    parts = rel_path.split(os.sep)
    category = parts[1].lower() if len(parts) > 1 else ""
    return "anime" in category and "movie" not in category


def hints_for_drive(drive_id, conn):
    """
    {node_id: [hint, ...]} for every flagged title on a drive.

    Two queries rather than one per title: titles at depth 2, then every
    depth-3 row at once, grouped by parent.
    """
    titles = conn.execute(
        "SELECT id, rel_path FROM nodes "
        "WHERE drive_id = ? AND depth = 2 AND is_dir = 1 AND root_type = 'library'",
        (drive_id,),
    ).fetchall()
    if not titles:
        return {}

    children = {}
    for row in conn.execute(
        "SELECT parent_id, name, is_dir FROM nodes WHERE drive_id = ? AND depth = 3",
        (drive_id,),
    ):
        children.setdefault(row["parent_id"], []).append((row["name"], row["is_dir"]))

    found = {}
    for title in titles:
        kids = children.get(title["id"])
        if not kids:
            continue
        hints = hints_for(kids, is_anime_category(title["rel_path"]))
        if hints:
            found[title["id"]] = hints
    return found


def describe(hints):
    """One line per hint, for a tooltip."""
    return "\n".join(f"{HINTS[h]['label']}: {HINTS[h]['detail']}"
                     for h in hints if h in HINTS)
