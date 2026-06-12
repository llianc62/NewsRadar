# coding=utf-8
"""
Keyword matching engine for news titles.

Reads ``frequency_words.txt`` and matches news titles against
keyword groups.  Supports plain words, ``/regex/`` patterns,
``!filter`` words, ``+required`` words, ``@N`` max counts, and
``=> Display Name`` aliases.

Previously named ``frequency.py`` to avoid shadowing the stdlib
``keyword`` module (which ``collections`` imports).  Now safely
named ``keywords.py`` inside the ``news`` package.

Usage::

    from news.keywords import load_frequency_words, match_and_group

    groups, filters, global_filters = load_frequency_words("frequency_words.txt")
    grouped = match_and_group(items, groups, global_filters, max_per_group=10)
"""

import re
from typing import Any, Dict, List, Optional, Tuple


# ── Parser ──────────────────────────────────────────────────────────

def load_frequency_words(
    filepath: str,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Parse ``frequency_words.txt`` and return keyword groups.

    File format::

        # Comments start with #
        [Group Alias]
        keyword1
        keyword2
        /regex pattern/
        !filter_word
        +required_word
        @5           # max items for this group
        keyword => Display Name

    Returns:
        (word_groups, filter_words, global_filters) tuple:

        * *word_groups*: list of group dicts with keys:
          ``name``, ``words``, ``regexes``, ``filter_words``,
          ``required_words``, ``max_count`` (0 = unlimited),
          ``display_name``.
        * *filter_words*: (unused legacy — kept for API compatibility).
        * *global_filters*: list of filter words that apply to ALL
          groups (defined before the first ``[Group]`` header).
    """
    word_groups: List[Dict[str, Any]] = []
    global_filters: List[str] = []
    current_group: Optional[Dict[str, Any]] = None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"[Keywords] File not found: {filepath}")
        return word_groups, [], global_filters

    for line in lines:
        line = line.strip()

        # Skip empty lines and comments
        if not line or line.startswith("#"):
            continue

        # Group header: [Group Name]
        if line.startswith("[") and line.endswith("]"):
            group_name = line[1:-1].strip()
            current_group = {
                "name": group_name,
                "display_name": group_name,
                "words": [],
                "regexes": [],
                "filter_words": [],
                "required_words": [],
                "max_count": 0,
            }
            word_groups.append(current_group)
            continue

        # If we haven't seen a group header yet, these are global filters
        if current_group is None:
            if line.startswith("!"):
                global_filters.append(line[1:].strip())
            continue

        # @N — max item count for group
        if line.startswith("@"):
            try:
                current_group["max_count"] = int(line[1:].strip())
            except ValueError:
                pass
            continue

        # => Display Name alias
        if "=>" in line:
            parts = line.split("=>", 1)
            keyword = parts[0].strip()
            display_name = parts[1].strip()
            _add_keyword(current_group, keyword)
            current_group["display_name"] = display_name
            continue

        # Regular keyword or special prefix
        _add_keyword(current_group, line)

    return word_groups, [], global_filters


def _add_keyword(group: Dict[str, Any], keyword: str) -> None:
    """Classify and add a keyword to the group."""
    kw = keyword.strip()
    if not kw:
        return

    if kw.startswith("!") and len(kw) > 1:
        group["filter_words"].append(kw[1:])
    elif kw.startswith("+") and len(kw) > 1:
        group["required_words"].append(kw[1:])
    elif kw.startswith("/") and kw.endswith("/") and len(kw) > 2:
        group["regexes"].append(re.compile(kw[1:-1]))
    else:
        group["words"].append(kw.lower())


# ── Matcher ─────────────────────────────────────────────────────────

def match_title(
    title: str,
    word_groups: List[Dict[str, Any]],
    global_filters: Optional[List[str]] = None,
) -> Optional[str]:
    """Match a single title against keyword groups.

    Returns the *display_name* of the first matching group, or
    ``None`` if no group matches.
    """
    if global_filters is None:
        global_filters = []

    title_lower = title.lower()

    # Global filters: if ANY global filter word appears in the title, skip
    for fw in global_filters:
        if fw.lower() in title_lower:
            return None

    for group in word_groups:
        # Group-level filter words: if ANY appears, skip this group
        if any(fw.lower() in title_lower for fw in group["filter_words"]):
            continue

        # Required words: ALL must appear
        if group["required_words"]:
            if not all(rw.lower() in title_lower for rw in group["required_words"]):
                continue

        # Check plain words
        if any(kw in title_lower for kw in group["words"]):
            return group["display_name"]

        # Check regexes
        if any(rx.search(title) for rx in group["regexes"]):
            return group["display_name"]

        # If group has NO words/regexes (i.e. only filter/required),
        # treat it as "always match" for titles that pass the filters
        if not group["words"] and not group["regexes"] and not group["required_words"]:
            return group["display_name"]

    return None


def match_and_group(
    items: List[Dict[str, Any]],
    word_groups: List[Dict[str, Any]],
    global_filters: Optional[List[str]] = None,
    max_per_group: int = 0,
) -> Dict[str, List[Dict[str, Any]]]:
    """Match a list of items and group them by keyword.

    Args:
        items: List of item dicts (must have ``"title"`` key).
        word_groups: Parsed keyword groups.
        global_filters: Global filter words.
        max_per_group: Global max items per group (0 = unlimited).
            Per-group ``@N`` limits override this.

    Returns:
        ``{group_display_name: [item, ...]}`` including an
        ``"__unmatched__"`` key for items that matched no group.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    group_counts: Dict[str, int] = {}

    # Pre-populate group slots
    for group in word_groups:
        name = group.get("display_name", group["name"])
        grouped[name] = []
        group_counts[name] = 0

    grouped["__unmatched__"] = []

    for item in items:
        title = item.get("title", "")
        matched_name = match_title(title, word_groups, global_filters)

        if matched_name is None:
            grouped["__unmatched__"].append(item)
            continue

        # Check limit
        group = next(
            (g for g in word_groups if g.get("display_name", g["name"]) == matched_name),
            None,
        )
        limit = 0
        if group:
            limit = group.get("max_count", 0) or max_per_group

        if limit > 0 and group_counts.get(matched_name, 0) >= limit:
            grouped["__unmatched__"].append(item)
        else:
            grouped[matched_name].append(item)
            group_counts[matched_name] = group_counts.get(matched_name, 0) + 1

    # Remove empty groups
    return {k: v for k, v in grouped.items() if v}
