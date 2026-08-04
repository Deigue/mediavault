"""
tagging.py - Smart default tags based on category folder name.

"""


def default_tags_for_category(category_name):
    """
    category_name is the immediate parent folder of a title, e.g. "00_Anime",
    "01_Movies", "02_TV Shows", "03_Anime Movies".
    """
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
