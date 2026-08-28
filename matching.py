"""
matching.py - compare title names that are written differently.

The same show arrives named a dozen ways: dots for spaces, a release group in
brackets, a resolution, a year, a season marker. Comparing the raw folder name
misses every one of those, so names are reduced to a core first and compared
on that.

Used by duplicate detection and by search, so a query finds a title however
its files happen to be punctuated.
"""

import os
import re

# Anything from a release name that says nothing about which title this is.
NOISE = {
    "1080p", "2160p", "720p", "480p", "4k", "uhd", "hd", "sd",
    "bluray", "bdrip", "brrip", "bd", "webrip", "web", "webdl", "hdtv", "dvdrip",
    "dvd", "remux", "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "xvid",
    "10bit", "8bit", "aac", "ac3", "dts", "ddp", "dd", "flac", "opus", "atmos",
    "dual", "audio", "dualaudio", "multi", "subs", "sub", "subbed", "dubbed",
    "eng", "engsub", "english", "japanese", "complete", "batch", "repack",
    "proper", "extended", "uncut", "internal", "limited", "imax", "nf", "amzn",
    "atvp", "dsnp", "hulu", "max", "gp", "yts", "mx", "rarbg", "judas", "ember",
    "the", "a", "an",
}

# Bracketed chunks are almost always release metadata, not the title.
BRACKETS = re.compile(r"[\[({][^\])}]*[\])}]")
# A trailing year, e.g. "(2019)" once brackets are gone.
YEAR = re.compile(r"\b(19|20)\d{2}\b")
# Season and episode markers, so "Show S01E02" reduces to "show".
SEASON_EP = re.compile(
    r"\b(?:s\d{1,2}(?:e\d{1,3})?|e\d{1,3}|ep\d{1,3}"
    r"|season\s*\d{1,2}|series\s*\d{1,2}|part\s*\d{1,2}|cour\s*\d{1,2}"
    r"|\d{1,2}x\d{1,3})\b",
    re.IGNORECASE,
)
SEPARATORS = re.compile(r"[._\-+~,:;!?'\"/\\]+")
NON_WORD = re.compile(r"[^a-z0-9 ]+")
SPACES = re.compile(r"\s+")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".webm"}


def normalise(name, keep_season=False):
    """
    A title name reduced to its comparable core.

    Strips the extension, bracketed release metadata, years, punctuation and
    the words that only describe the encode.

    keep_season decides what happens to "Season 2" and "S01E03", and the two
    callers want opposite things. Search drops them, so looking for a show
    finds every season and every episode of it. Duplicate detection keeps
    them, because without that "Show" and "Show Season 2" compare equal and
    one season gets reported as a backup of another, which is the one mistake
    this must never make.
    """
    if not name:
        return ""

    text = name
    root, ext = os.path.splitext(text)
    if ext.lower() in VIDEO_EXTENSIONS or ext.lower() in (".srt", ".vtt", ".nfo", ".ass"):
        text = root

    text = BRACKETS.sub(" ", text)
    text = SEPARATORS.sub(" ", text)
    text = text.lower()
    if keep_season:
        # Normalise the many spellings to one, rather than removing them.
        text = SEASON_EP.sub(lambda m: " " + _season_key(m.group(0)) + " ", text)
    else:
        text = SEASON_EP.sub(" ", text)
    text = YEAR.sub(" ", text)
    text = NON_WORD.sub(" ", text)

    keep = NOISE if not keep_season else NOISE - {"the", "a", "an"}
    words = [w for w in SPACES.split(text) if w and w not in keep]
    return " ".join(words)


# "S2", "Season 02" and "2nd Season" all mean the same season, so they reduce
# to one token. Without this the same season written two ways looks like two
# different works.
ORDINAL = re.compile(r"^(\d+)")


def _season_key(marker):
    """One canonical token for a season or episode marker."""
    text = marker.lower().strip()
    numbers = re.findall(r"\d+", text)
    if not numbers:
        return "s"
    if "e" in text.split("s")[-1] and len(numbers) >= 2:
        return f"s{int(numbers[0])}e{int(numbers[1])}"
    if text.startswith("e") or text.startswith("ep"):
        return f"e{int(numbers[0])}"
    if "part" in text:
        return f"p{int(numbers[0])}"
    return f"s{int(numbers[0])}"


def title_key(name):
    """
    The key duplicate detection groups on. Seasons are kept distinct.
    """
    return normalise(name, keep_season=True)


def tokens(name):
    """The distinct words of a normalised name."""
    return set(normalise(name).split())


def same_title(a, b):
    """
    Do these two names refer to the same work?

    Exact match on the normalised form, or one being wholly contained in the
    other, which covers a title stored beside its own season folders.
    """
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return ta <= tb or tb <= ta


def matches_query(query, name):
    """
    Would someone searching `query` expect `name` back?

    Every word of the query has to appear in the name, compared on the
    normalised form, so "prison break" finds "Prison.Break.S01E03.1080p".
    Falls back to a plain substring test, which is what makes a search for a
    release group or a year still work.
    """
    wanted = normalise(query).split()
    if not wanted:
        return query.lower() in (name or "").lower()
    haystack = normalise(name)
    if all(w in haystack for w in wanted):
        return True
    return query.lower() in (name or "").lower()
