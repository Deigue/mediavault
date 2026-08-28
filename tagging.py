"""
tagging.py - default tags from a category folder name, plus the two tags the
suggestion engine acts on.
"""


# Neither system tag is ever applied automatically. They only mean anything
# because you put them there.
STAR_TAG = "star"
WATCHING_TAG = "watching"

# Marks a title you have deliberately only backed up part of, so the amber
# shield stops nagging. Not a toggle like the two above: it is answering a
# question the shield asked, so it belongs with the ordinary tags, just
# offered in the list rather than typed out.
PARTIAL_OK_TAG = "partial-ok"

KNOWN_TAGS = {
    PARTIAL_OK_TAG: "Backing up only part of this title is deliberate, so "
                    "count it as covered.",
}

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
# lands in the first bucket that matches.
#
# Anime films count as movies, the way the tracking sites treat them, so the
# top level reads shows / movies / anime rather than carrying a fourth label
# that is only ever a handful of titles. The Anime Movies folder is untouched
# and those titles still carry both tags, so filtering for them still works.
CATEGORY_BUCKETS = [
    ("tv", "shows", lambda n: "tv" in n or "show" in n or "series" in n),
    ("movies", "movies", lambda n: "movie" in n or "film" in n),
    ("anime", "anime", lambda n: "anime" in n),
    ("other", "other", lambda _n: True),
]

# Anime is already a mass noun, so it is the same either way.
SINGULAR = {"shows": "show", "movies": "movie", "anime": "anime", "other": "other"}


def bucket_for_category(category_name):
    """Which counter a category folder belongs to. Returns (key, label)."""
    name = (category_name or "").lower()
    for key, label, matches in CATEGORY_BUCKETS:
        if matches(name):
            return key, label
    return "other", "other"


def count_label(label, n):
    """The label to print beside a number, singular where that reads better."""
    return SINGULAR.get(label, label) if n == 1 else label
