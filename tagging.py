"""
tagging.py - default tags from a category folder name, plus the two tags the
suggestion engine acts on.
"""


# Neither system tag is ever applied automatically. They only mean anything
# because you put them there.
STAR_TAG = "star"
WATCHING_TAG = "watching"

SYSTEM_TAGS = {
    STAR_TAG: {
        "label": "Star",
        "symbol": "★",
        "meaning": "Worth protecting. Starred titles with no copy anywhere are "
                   "what the suggestions offer to back up.",
    },
    WATCHING_TAG: {
        "label": "Watching",
        "symbol": "▶",
        "meaning": "In use right now. Never suggested for a move, however old "
                   "the files are.",
    },
}


def is_system_tag(tag):
    return (tag or "").strip().lower() in SYSTEM_TAGS


def has_tag(tags, wanted):
    return any((t or "").strip().lower() == wanted for t in (tags or []))


def default_tags_for_category(category_name):
    """category_name is a title's parent folder, e.g. "00_Anime" or "Movies"."""
    name = category_name.lower()
    is_anime = "anime" in name
    is_movie = "movie" in name or "film" in name
    is_tv = "tv" in name or "show" in name or "series" in name

    tags = []
    if is_movie:
        tags.append("Movie")
    if is_tv:
        tags.append("TV")
    if is_anime:
        tags.append("Anime")
    return tags


# One bucket per title for the counters, in the order they are shown. A title
# lands in the first bucket that matches, so "Anime Movies" is not counted
# twice.
CATEGORY_BUCKETS = [
    ("anime", "anime", lambda n: "anime" in n and not ("movie" in n or "film" in n)),
    ("anime-movies", "anime films", lambda n: "anime" in n),
    ("tv", "TV shows", lambda n: "tv" in n or "show" in n or "series" in n),
    ("movies", "movies", lambda n: "movie" in n or "film" in n),
    ("other", "other", lambda _n: True),
]


def bucket_for_category(category_name):
    """Which counter a category folder belongs to. Returns (key, label)."""
    name = (category_name or "").lower()
    for key, label, matches in CATEGORY_BUCKETS:
        if matches(name):
            return key, label
    return "other", "other"
