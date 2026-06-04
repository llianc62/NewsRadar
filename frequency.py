# coding=utf-8
"""
Keyword matching engine for news title filtering.

Parses frequency_words.txt configuration and matches news titles against
keyword groups. Supports:
- Plain keywords (substring, case-insensitive)
- Regular expressions (/pattern/ syntax with optional flags)
- Required words (+prefix, ALL must match)
- Filter words (!prefix, matches exclude the title from the group)
- Group alias ([Group Name] as first line of a block)
- Display aliases (keyword => Display Name)
- Max item limits (@N per group)
- Global filter section ([GLOBAL_FILTER])

NOTE: This file is deliberately named keyword.py.  It shadows the stdlib
``keyword`` module.  To avoid ImportError when a stdlib module (notably
``collections``) tries to ``from keyword import iskeyword``, all imports
that transitively depend on stdlib ``keyword`` are deferred inside
functions.  The helper ``_import_re()`` temporarily evicts this module
from ``sys.modules`` while ``re`` (and therefore ``collections``) is
being loaded, then restores it.
"""

import sys

# Do NOT add top-level imports of ``re``, ``typing``, or any module that
# transitively imports ``collections`` (which imports stdlib ``keyword``).
# Use ``_import_re()`` below when you need regular expressions.


# ── Internal helpers ────────────────────────────────────────────


def _import_re():
    """Lazily import the stdlib ``re`` module.

    Because this file is named ``keyword.py`` it shadows the stdlib
    ``keyword`` module.  ``collections`` (and therefore anything that
    imports it) needs ``from keyword import iskeyword``.  We temporarily
    remove ourselves from ``sys.modules`` and prune cwd from
    ``sys.path`` so the stdlib ``keyword`` can be found.

    Returns:
        The ``re`` module.
    """
    if "re" in sys.modules:
        return sys.modules["re"]

    me = sys.modules.pop("keyword", None)
    saved_path = list(sys.path)
    # Keep only stdlib / site-packages paths; remove cwd and project dirs.
    sys.path[:] = [p for p in sys.path if p not in ("",) and "newsnow-crawler" not in p]

    try:
        import re as _re
    finally:
        sys.path[:] = saved_path
        if me is not None:
            sys.modules["keyword"] = me

    return _re


def _parse_word(word: str) -> dict:
    """Parse a single word config line.

    Detects /regex/ patterns and => display_name aliases.

    Args:
        word: Raw config line, e.g. "/京东|刘强东/ => 京东"

    Returns:
        Dict with keys: word, is_regex, pattern, display_name
    """
    _re = _import_re()
    display_name = None

    # 1. Handle display name (=>)
    if "=>" in word:
        parts = _re.split(r"\s*=>\s*", word, 1)
        word_config = parts[0].strip()
        if len(parts) > 1 and parts[1].strip():
            display_name = parts[1].strip()
    else:
        word_config = word.strip()

    # 2. Check for regex pattern: /pattern/flags
    regex_match = _re.match(r"^/(.+)/[a-z]*$", word_config)

    if regex_match:
        pattern_str = regex_match.group(1)
        try:
            pattern = _re.compile(pattern_str, _re.IGNORECASE)
            return {
                "word": pattern_str,
                "is_regex": True,
                "pattern": pattern,
                "display_name": display_name,
            }
        except _re.error as e:
            print(f"Warning: Invalid regex pattern '/{pattern_str}/': {e}")

    return {
        "word": word_config,
        "is_regex": False,
        "pattern": None,
        "display_name": display_name,
    }


def _word_matches(word_config: dict, title_lower: str) -> bool:
    """Check if a parsed word config matches a lowercase title.

    Args:
        word_config: Dict from _parse_word with is_regex/pattern or word key.
        title_lower: Lowercase title string.

    Returns:
        True if the word matches the title.
    """
    if word_config.get("is_regex") and word_config.get("pattern"):
        return bool(word_config["pattern"].search(title_lower))
    else:
        return word_config["word"].lower() in title_lower


def _parse_group(lines: list) -> dict | None:
    """Parse a single keyword group block.

    Args:
        lines: List of non-empty, non-comment lines in the group.

    Returns:
        Group dict with keys: key, display_name, normal, required, filters, max_count.
        Returns None if the group has no usable keywords.
    """
    group_alias = None
    group_required = []
    group_normal = []
    group_filters = []
    group_max_count = 0

    # Check for group alias: [Group Name]
    if lines and lines[0].startswith("[") and lines[0].endswith("]"):
        alias = lines[0][1:-1].strip()
        section_upper = alias.upper()
        if section_upper not in ("GLOBAL_FILTER", "WORD_GROUPS"):
            group_alias = alias
            lines = lines[1:]

    for line in lines:
        if line.startswith("@"):
            # Max display count
            try:
                count = int(line[1:])
                if count > 0:
                    group_max_count = count
            except (ValueError, IndexError):
                pass
        elif line.startswith("!"):
            # Per-group filter word
            filter_word = line[1:]
            group_filters.append(_parse_word(filter_word))
        elif line.startswith("+"):
            # Required word
            req_word = line[1:]
            group_required.append(_parse_word(req_word))
        else:
            # Normal word
            group_normal.append(_parse_word(line))

    if not group_normal and not group_required:
        return None

    # Generate group key from normal words (or required if no normal)
    if group_normal:
        group_key = " ".join(w["word"] for w in group_normal)
    else:
        group_key = " ".join(w["word"] for w in group_required)

    # Generate display name
    # Priority: group alias > display aliases joined by " / " > keywords joined
    if group_alias:
        display_name = group_alias
    else:
        all_words = group_normal + group_required
        display_parts = []
        for w in all_words:
            part = w.get("display_name") or w["word"]
            display_parts.append(part)
        display_name = " / ".join(display_parts) if display_parts else None

    return {
        "key": group_key,
        "display_name": display_name,
        "normal": group_normal,
        "required": group_required,
        "filters": group_filters,
        "max_count": group_max_count,
    }


# ── Public API ──────────────────────────────────────────────────


def load_frequency_words(
    filepath: str = "frequency_words.txt",
) -> tuple[list[dict], list[str], list[str]]:
    """Load and parse a frequency_words.txt configuration file.

    The file has two sections:
      [GLOBAL_FILTER] — global exclusion words/regexes
      [WORD_GROUPS]   — keyword groups separated by blank lines

    Each group supports:
      - Plain keywords (substring match, case-insensitive)
      - /regex/ patterns (compiled with re.IGNORECASE)
      - keyword => Display Name (display alias)
      - [Group Alias] as the first line (group display name)
      - +keyword (required — ALL must match)
      - !keyword (per-group filter — match excludes title from group)
      - @N (max items for the group)

    Args:
        filepath: Path to the frequency_words.txt file.

    Returns:
        Tuple of (word_groups, filter_words, global_filters):
        - word_groups: List of group dicts, each with keys:
            key, display_name, normal, required, filters, max_count
        - filter_words: List of accumulated filter word strings (for inspection)
        - global_filters: List of global filter strings (plain words or /regex/)

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Split into blocks by double newline
    blocks = [block.strip() for block in content.split("\n\n") if block.strip()]

    processed_groups = []
    filter_words = []
    global_filters = []
    current_section = "WORD_GROUPS"

    for block in blocks:
        # Remove comments and empty lines
        lines = [
            line.strip()
            for line in block.split("\n")
            if line.strip() and not line.strip().startswith("#")
        ]

        if not lines:
            continue

        # Check for section marker
        if lines[0].startswith("[") and lines[0].endswith("]"):
            section_name = lines[0][1:-1].upper()
            if section_name in ("GLOBAL_FILTER", "WORD_GROUPS"):
                current_section = section_name
                lines = lines[1:]

        if current_section == "GLOBAL_FILTER":
            for line in lines:
                # Skip special prefix syntax in global filter (not supported)
                if line.startswith(("!", "+", "@")):
                    continue
                if line:
                    global_filters.append(line)
            continue

        # WORD_GROUPS section
        parsed = _parse_group(lines)
        if parsed:
            processed_groups.append(parsed)
            # Accumulate filter word strings for the top-level return
            for fw in parsed["filters"]:
                filter_words.append(fw["word"])

    return processed_groups, filter_words, global_filters


def match_title(
    title: str,
    word_groups: list[dict],
    global_filters: list[str],
) -> str | None:
    """Check if a title matches any keyword group.

    Args:
        title: The news title to check.
        word_groups: List of group dicts from load_frequency_words.
        global_filters: List of global filter strings.

    Returns:
        The matched group's display_name, or None if no group matched
        (or if excluded by global filters).
    """
    if not isinstance(title, str):
        title = str(title) if title is not None else ""
    if not title.strip():
        return None

    title_lower = title.lower()

    # 1. Global filters: any match excludes the title entirely
    for gf_text in global_filters:
        parsed = _parse_word(gf_text)
        if _word_matches(parsed, title_lower):
            return None

    # 2. Check each group
    for group in word_groups:
        # Per-group filters: any match skips this group
        if group["filters"]:
            if any(_word_matches(f, title_lower) for f in group["filters"]):
                continue

        # Required words: ALL must match
        if group["required"]:
            if not all(
                _word_matches(r, title_lower) for r in group["required"]
            ):
                continue

        # Normal words: ANY must match
        if group["normal"]:
            if not any(
                _word_matches(n, title_lower) for n in group["normal"]
            ):
                continue

        return group["display_name"]

    return None


def match_and_group(
    items: list[dict],
    word_groups: list[dict],
    global_filters: list[str],
    max_per_group: int = 0,
) -> dict[str, list[dict]]:
    """Group a list of news items by keyword matching.

    Each item should be a dict with at least a 'title' key.

    Args:
        items: List of item dicts, each must have a 'title' key.
        word_groups: List of group dicts from load_frequency_words.
        global_filters: List of global filter strings.
        max_per_group: Global per-group item limit (0 = no limit).
            Per-group @N limits take precedence over this value.

    Returns:
        Dict mapping group display_name -> list of matched items.
        Unmatched items are placed under the key "__unmatched__".
    """
    result: dict[str, list[dict]] = {"__unmatched__": []}

    # Initialize buckets for each group
    for group in word_groups:
        name = group["display_name"]
        if name not in result:
            result[name] = []

    for item in items:
        title = item.get("title", "")
        matched_name = match_title(title, word_groups, global_filters)

        if matched_name:
            result[matched_name].append(item)
        else:
            result["__unmatched__"].append(item)

    # Apply per-group max counts
    for group in word_groups:
        name = group["display_name"]
        # Per-group @N takes precedence, fall back to global max_per_group
        limit = group.get("max_count", 0) or max_per_group
        if limit > 0 and len(result[name]) > limit:
            result[name] = result[name][:limit]

    return result
