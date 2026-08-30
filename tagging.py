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


# A second classification, for how much losing one title would hurt. Kept
# apart from CATEGORY_BUCKETS on purpose: those are the counters on the page
# and moving titles between them would change figures nobody asked to change.
# Here anime films are their own bucket rather than movies, because what a
# backup of one is worth has nothing to do with how the sites file them.
#
# Order matters, first match wins, so anime films are tested before movies.
WEIGHT_BUCKETS = [
    ("anime_movies", "anime films",
     lambda n: "anime" in n and ("movie" in n or "film" in n)),
    ("tv", "shows", lambda n: "tv" in n or "show" in n or "series" in n),
    ("movies", "movies", lambda n: "movie" in n or "film" in n),
    ("anime", "anime", lambda n: "anime" in n),
    ("other", "other", lambda _n: True),
]

WEIGHT_BUCKET_KEYS = [key for key, _label, _match in WEIGHT_BUCKETS]

WEIGHT_BUCKET_LABELS = {key: label for key, label, _match in WEIGHT_BUCKETS}


def weight_bucket_for_category(category_name):
    """Which weight bucket a category folder falls in. Returns the key."""
    name = (category_name or "").lower()
    for key, _label, matches in WEIGHT_BUCKETS:
        if matches(name):
            return key
    return "other"


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
