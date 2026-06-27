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

def _new_group(name: Optional[str]) -> Dict[str, Any]:
    """Create a fresh keyword group dict."""
    return {
        "name": name,
        "display_name": name,
        "words": [],
        "regexes": [],
        "filter_words": [],
        "required_words": [],
        "max_count": 0,
    }


def _group_has_content(group: Dict[str, Any]) -> bool:
    """Return True if the group has any matchable content."""
    return bool(
        group["words"] or group["regexes"]
        or group["required_words"] or group["filter_words"]
    )


def load_frequency_words(
    filepath: str,
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """Parse ``frequency_words.txt`` and return keyword groups.

    Groups are separated by blank lines.  ``[Group Name]`` headers
    name a group (and implicitly start one).  Lines with ``=>`` set
    the group's ``display_name``.

    Returns:
        (word_groups, filter_words, global_filters) tuple:

        * *word_groups*: list of group dicts with keys:
          ``name``, ``words``, ``regexes``, ``filter_words``,
          ``required_words``, ``max_count`` (0 = unlimited),
          ``display_name``.
        * *filter_words*: (unused legacy — kept for API compatibility).
        * *global_filters*: list of filter words that apply to ALL
          groups (``!word`` lines before the first ``[Group]``).
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

        # Comments are always skipped
        if line.startswith("#"):
            continue

        # ── Blank line → close current group ──────────────────────
        if not line:
            current_group = None
            continue

        # ── Group header: [Group Name] ────────────────────────────
        if line.startswith("[") and line.endswith("]"):
            group_name = line[1:-1].strip()
            current_group = _new_group(group_name)
            word_groups.append(current_group)
            continue

        # ── Legacy: !word before any group → global filter ────────
        if current_group is None and not word_groups:
            if line.startswith("!"):
                global_filters.append(line[1:].strip())
                continue

        # ── Start anonymous group if needed ───────────────────────
        if current_group is None:
            current_group = _new_group(None)
            word_groups.append(current_group)

        # ── @N — max item count for group ─────────────────────────
        if line.startswith("@"):
            try:
                current_group["max_count"] = int(line[1:].strip())
            except ValueError:
                pass
            continue

        # ── => Display Name alias ─────────────────────────────────
        if "=>" in line:
            parts = line.split("=>", 1)
            keyword = parts[0].strip()
            display_name = parts[1].strip()
            _add_keyword(current_group, keyword)
            # Per docs, [Group Name] takes priority over => alias:
            # "显示名称优先级: 1. 有组别名 → 显示组别名"
            # Only apply => display_name when there is no header.
            if current_group["name"] is None:
                current_group["display_name"] = display_name
                current_group["name"] = display_name
            continue

        # ── Regular keyword or special prefix ─────────────────────
        _add_keyword(current_group, line)

    # ── Extract [GLOBAL_FILTER] words → global_filters ───────────
    # Per the config file docs, [GLOBAL_FILTER] defines words that
    # EXCLUDE matching titles from ALL groups.  Its keywords (plain
    # words, not !word) become global filters.
    gf_idx = next(
        (i for i, g in enumerate(word_groups) if g["name"] == "GLOBAL_FILTER"),
        None,
    )
    if gf_idx is not None:
        gf_group = word_groups.pop(gf_idx)
        global_filters.extend(gf_group["words"])
        # Also handle any regex patterns defined under GLOBAL_FILTER
        for rx in gf_group["regexes"]:
            global_filters.append(rx.pattern)

    # Derive names for anonymous groups, then drop empty groups
    for g in word_groups:
        if g["display_name"] is None:
            if g["words"]:
                g["display_name"] = " / ".join(g["words"][:3])
            elif g["regexes"]:
                g["display_name"] = g["regexes"][0].pattern[:30]
            else:
                g["display_name"] = "未命名"
        if g["name"] is None:
            g["name"] = g["display_name"]
    word_groups = [g for g in word_groups if _group_has_content(g)]

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
    elif kw.startswith("/") and len(kw) > 2:
        # Support /pattern/ and /pattern/i
        # Per the config file docs: « /正则/ 正则表达式匹配（自动忽略大小写） »
        # IGNORECASE is the default; adding /i is explicit-but-redundant.
        last_slash = kw.rfind("/")
        if last_slash > 0:
            pattern = kw[1:last_slash]
            flags = re.IGNORECASE
            group["regexes"].append(re.compile(pattern, flags))
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
        # Note: required_words are checked above and already passed.
        if not group["words"] and not group["regexes"]:
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
