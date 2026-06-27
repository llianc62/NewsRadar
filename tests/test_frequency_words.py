# coding=utf-8
"""Tests for the frequency_words.txt parser and keyword matching engine.

Covers:
    - load_frequency_words() — parsing rules
    - match_title()         — single-title matching
    - match_and_group()     — batch grouping
    - _add_keyword()        — keyword classification
"""

import re
import tempfile
from pathlib import Path

import pytest

from news.keywords import (
    _add_keyword,
    _new_group,
    _group_has_content,
    load_frequency_words,
    match_title,
    match_and_group,
)


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _write_config(content: str) -> Path:
    """Write *content* to a temporary file and return its path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# ═══════════════════════════════════════════════════════════════════════
# _new_group / _group_has_content 单元测试
# ═══════════════════════════════════════════════════════════════════════

def test_new_group_has_correct_defaults():
    g = _new_group("测试")
    assert g["name"] == "测试"
    assert g["display_name"] == "测试"
    assert g["words"] == []
    assert g["regexes"] == []
    assert g["filter_words"] == []
    assert g["required_words"] == []
    assert g["max_count"] == 0


def test_new_group_anonymous():
    g = _new_group(None)
    assert g["name"] is None
    assert g["display_name"] is None


def test_group_has_content_words():
    g = _new_group(None)
    assert not _group_has_content(g)
    g["words"].append("test")
    assert _group_has_content(g)


def test_group_has_content_regexes():
    g = _new_group(None)
    g["regexes"].append(re.compile("test"))
    assert _group_has_content(g)


def test_group_has_content_filters():
    g = _new_group(None)
    g["filter_words"].append("exclude")
    assert _group_has_content(g)


def test_group_has_content_required():
    g = _new_group(None)
    g["required_words"].append("must")
    assert _group_has_content(g)


# ═══════════════════════════════════════════════════════════════════════
# _add_keyword 单元测试
# ═══════════════════════════════════════════════════════════════════════

class TestAddKeyword:
    def test_plain_word(self):
        g = _new_group(None)
        _add_keyword(g, "华为")
        assert g["words"] == ["华为"]
        assert g["regexes"] == []

    def test_plain_word_lowered(self):
        g = _new_group(None)
        _add_keyword(g, "Huawei")
        assert g["words"] == ["huawei"]

    def test_filter_word(self):
        g = _new_group(None)
        _add_keyword(g, "!水果")
        assert g["filter_words"] == ["水果"]

    def test_required_word(self):
        g = _new_group(None)
        _add_keyword(g, "+发布会")
        assert g["required_words"] == ["发布会"]

    def test_regex_basic(self):
        g = _new_group(None)
        _add_keyword(g, "/华为|鸿蒙/")
        assert g["words"] == []
        assert len(g["regexes"]) == 1
        assert g["regexes"][0].flags & re.IGNORECASE

    def test_regex_with_i_flag(self):
        """Explicit /i flag is still supported (redundant but harmless)."""
        g = _new_group(None)
        _add_keyword(g, "/claude|anthropic/i")
        assert g["regexes"][0].flags & re.IGNORECASE
        assert g["regexes"][0].search("Claude")

    def test_regex_word_boundary(self):
        g = _new_group(None)
        _add_keyword(g, r"/\bAI\b/")
        assert g["regexes"][0].search("AI is great")
        assert not g["regexes"][0].search("MAIL")

    def test_skip_exclamation_single_char(self):
        g = _new_group(None)
        _add_keyword(g, "!")
        assert g["words"] == ["!"]  # falls through to plain word

    def test_skip_plus_single_char(self):
        g = _new_group(None)
        _add_keyword(g, "+")
        assert g["words"] == ["+"]  # falls through to plain word

    def test_skip_empty_keyword(self):
        g = _new_group(None)
        _add_keyword(g, "  ")
        assert g["words"] == []


# ═══════════════════════════════════════════════════════════════════════
# load_frequency_words — 解析规则
# ═══════════════════════════════════════════════════════════════════════

class TestLoadFrequencyWords:
    """Parser tests — verify the rules documented in frequency_words.txt."""

    # ── Comments ──────────────────────────────────────────────────

    def test_comments_skipped(self):
        path = _write_config("# 这是注释\n华为\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 1
        assert groups[0]["words"] == ["华为"]

    # ── Blank-line separation ────────────────────────────────────

    def test_single_blank_line_separates_groups(self):
        path = _write_config("华为\n\n苹果\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 2
        assert groups[0]["words"] == ["华为"]
        assert groups[1]["words"] == ["苹果"]

    def test_multiple_blank_lines_same_as_one(self):
        path = _write_config("华为\n\n\n\n苹果\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 2

    def test_trailing_blank_lines_ignored(self):
        path = _write_config("华为\n\n\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 1

    def test_leading_blank_lines_ignored(self):
        path = _write_config("\n\n\n华为\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 1

    # ── [Group Name] headers ──────────────────────────────────────

    def test_group_header_names_group(self):
        path = _write_config("[华为]\n华为\n鸿蒙\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 1
        assert groups[0]["name"] == "华为"
        assert groups[0]["display_name"] == "华为"

    def test_group_header_closes_previous_and_starts_new(self):
        """[Group] is NOT a blank line — it directly closes prev and starts new."""
        path = _write_config("华为\n[苹果]\niPhone\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 2
        assert groups[0]["name"] == "华为"  # auto-named from keyword
        assert groups[1]["name"] == "苹果"

    # ── => display alias ──────────────────────────────────────────

    def test_arrow_sets_display_name(self):
        path = _write_config("/华为|鸿蒙/ => 华为\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["display_name"] == "华为"
        assert groups[0]["name"] == "华为"

    def test_arrow_does_not_override_header(self):
        """Per docs: 有组别名 → 显示组别名 (header takes priority)."""
        path = _write_config("[华为公司]\n/华为|鸿蒙/ => 华为\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["display_name"] == "华为公司"  # header preserved

    def test_arrow_adds_keyword_even_when_header_exists(self):
        """=> still adds the regex/word; only display_name is skipped."""
        path = _write_config("[华为公司]\n/华为|鸿蒙/ => 华为\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups[0]["regexes"]) == 1

    # ── !filter and +required ─────────────────────────────────────

    def test_filter_word_exclusion(self):
        path = _write_config("[苹果]\n苹果\n!水果\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["filter_words"] == ["水果"]

    def test_required_word(self):
        path = _write_config("+苹果\n+发布会\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["required_words"] == ["苹果", "发布会"]

    # ── @N max count ──────────────────────────────────────────────

    def test_max_count(self):
        path = _write_config("[科技]\n科技\n@5\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["max_count"] == 5

    def test_max_count_invalid_ignored(self):
        path = _write_config("[科技]\n科技\n@abc\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["max_count"] == 0

    # ── Regex patterns ────────────────────────────────────────────

    def test_regex_case_insensitive_by_default(self):
        """Per docs: « /正则/ 正则表达式匹配（自动忽略大小写） »"""
        path = _write_config("/huanwei|honor/\n")
        groups, _, _ = load_frequency_words(str(path))
        rx = groups[0]["regexes"][0]
        assert rx.flags & re.IGNORECASE
        assert rx.search("HUANWEI")

    def test_regex_case_insensitive_explicit_i(self):
        path = _write_config("/huawei|honor/i\n")
        groups, _, _ = load_frequency_words(str(path))
        rx = groups[0]["regexes"][0]
        assert rx.flags & re.IGNORECASE

    # ── Anonymous group naming ────────────────────────────────────

    def test_anonymous_group_named_from_words(self):
        path = _write_config("华为\n鸿蒙\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["display_name"] == "华为 / 鸿蒙"

    def test_anonymous_group_named_from_regexes(self):
        path = _write_config("/华为|鸿蒙/\n")
        groups, _, _ = load_frequency_words(str(path))
        assert "华为|鸿蒙" in groups[0]["display_name"]

    def test_anonymous_group_unnamed_fallback(self):
        """Group with only filters/required (no words/regexes) gets placeholder.

        A ``!word``-only group must appear AFTER the first real group
        (otherwise it is treated as a legacy global filter).
        """
        path = _write_config("华为\n\n!水果\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups[0]["display_name"] == "华为"  # first group
        assert groups[1]["display_name"] == "未命名"  # filter-only group

    # ── [GLOBAL_FILTER] → global_filters ─────────────────────────

    def test_global_filter_words_become_exclusion_filters(self):
        path = _write_config("[GLOBAL_FILTER]\n震惊\n标题党\n")
        groups, _, global_filters = load_frequency_words(str(path))
        assert "震惊" in global_filters
        assert "标题党" in global_filters
        # GLOBAL_FILTER group itself is removed from word_groups
        names = [g["name"] for g in groups]
        assert "GLOBAL_FILTER" not in names

    def test_global_filter_regexes_become_exclusion_patterns(self):
        path = _write_config("[GLOBAL_FILTER]\n/赌博|博彩/\n")
        groups, _, global_filters = load_frequency_words(str(path))
        assert "赌博|博彩" in global_filters

    # ── [WORD_GROUPS] is silently ignored ─────────────────────────

    def test_word_groups_header_ignored(self):
        """[WORD_GROUPS] has no content → filtered by _group_has_content."""
        path = _write_config("华为\n\n[WORD_GROUPS]\n\n苹果\n")
        groups, _, _ = load_frequency_words(str(path))
        names = [g["name"] for g in groups]
        assert "WORD_GROUPS" not in names
        assert len(groups) == 2  # 华为, 苹果

    # ── Legacy: !word before any group ────────────────────────────

    def test_legacy_global_filter(self):
        path = _write_config("!广告\n!垃圾\n\n华为\n")
        groups, _, global_filters = load_frequency_words(str(path))
        assert "广告" in global_filters
        assert "垃圾" in global_filters
        assert len(groups) == 1  # only the 华为 group

    # ── Edge cases ────────────────────────────────────────────────

    def test_empty_file(self):
        path = _write_config("")
        groups, _, global_filters = load_frequency_words(str(path))
        assert groups == []
        assert global_filters == []

    def test_file_not_found(self):
        groups, _, _ = load_frequency_words("/nonexistent/path.txt")
        assert groups == []

    def test_only_comments(self):
        path = _write_config("# 注释1\n# 注释2\n")
        groups, _, _ = load_frequency_words(str(path))
        assert groups == []

    def test_content_after_header_no_blank_line(self):
        """Content on line after header: same group."""
        path = _write_config("[华为]\n手机\n5G\n")
        groups, _, _ = load_frequency_words(str(path))
        assert len(groups) == 1
        assert "手机" in groups[0]["words"]

    def test_realistic_config_scenario(self):
        """Integration: the pattern actually used in frequency_words.txt."""
        path = _write_config(
            "[GLOBAL_FILTER]\n"
            "震惊\n"
            "\n"
            "/胖东来|于东来/ => 胖东来\n"
            "\n"
            "/深度求索|幻方量化|梁文锋|\\bDeepSeek\\b/ => DeepSeek\n"
            "\n"
            "[中国]\n"
            "国产\n"
            "中国\n"
            "\n"
            "印度\n"
            "\n"
            "[AI 相关]\n"
            "人工智能\n"
            "/(?<![a-zA-Z])ai(?![a-zA-Z])/\n"
        )
        groups, _, global_filters = load_frequency_words(str(path))
        assert "震惊" in global_filters
        assert len(groups) == 5  # 胖东来, DeepSeek, 中国, 印度, AI 相关
        # 胖东来
        assert groups[0]["display_name"] == "胖东来"
        # DeepSeek
        assert groups[1]["display_name"] == "DeepSeek"
        # 中国
        assert groups[2]["name"] == "中国"
        # 印度 (anonymous)
        assert groups[3]["words"] == ["印度"]
        # AI 相关
        assert groups[4]["name"] == "AI 相关"


# ═══════════════════════════════════════════════════════════════════════
# match_title — 单标题匹配
# ═══════════════════════════════════════════════════════════════════════

class TestMatchTitle:
    """Matching semantics tests."""

    # ── Plain word ────────────────────────────────────────────────

    def test_plain_word_match(self):
        groups = [_new_group("华为")]
        groups[0]["words"] = ["华为"]
        assert match_title("华为发布新手机", groups) == "华为"

    def test_plain_word_no_match(self):
        groups = [_new_group("华为")]
        groups[0]["words"] = ["华为"]
        assert match_title("苹果发布新手机", groups) is None

    def test_case_insensitive_english(self):
        groups = [_new_group("AI")]
        groups[0]["words"] = ["ai"]  # stored as lowercase
        assert match_title("Latest AI Breakthrough", groups) == "AI"

    # ── Regex ─────────────────────────────────────────────────────

    def test_regex_match(self):
        g = _new_group("芯片")
        g["regexes"] = [re.compile("芯片|半导体", re.IGNORECASE)]
        assert match_title("半导体行业报告", [g]) == "芯片"

    def test_regex_case_insensitive(self):
        g = _new_group("测试")
        g["regexes"] = [re.compile("Huawei", re.IGNORECASE)]
        assert match_title("HUAWEI Mate 60", [g]) == "测试"

    # ── Filter words ──────────────────────────────────────────────

    def test_filter_word_excludes(self):
        g = _new_group("苹果公司")
        g["words"] = ["苹果"]
        g["filter_words"] = ["水果"]
        assert match_title("苹果发布新手机", [g]) == "苹果公司"
        assert match_title("苹果水果丰收", [g]) is None

    # ── Required words ────────────────────────────────────────────

    def test_required_words_all_must_match(self):
        g = _make_group("必须组", required_words=["苹果", "发布会"])
        assert match_title("苹果春季发布会即将召开", [g]) == "必须组"
        # Only matches one
        assert match_title("苹果股价上涨", [g]) is None

    # ── Global filters ────────────────────────────────────────────

    def test_global_filter_excludes_from_all_groups(self):
        g = _new_group("华为")
        g["words"] = ["华为"]
        assert match_title("震惊！华为发布新机", [g], global_filters=["震惊"]) is None

    def test_global_filter_case_insensitive(self):
        g = _new_group("华为")
        g["words"] = ["华为"]
        assert match_title("Breaking News 华为", [g], global_filters=["breaking news"]) is None

    # ── First-match-wins ──────────────────────────────────────────

    def test_first_group_wins(self):
        g1 = _new_group("第一")
        g1["words"] = ["华为"]
        g2 = _new_group("第二")
        g2["words"] = ["华为"]
        assert match_title("华为发布新手机", [g1, g2]) == "第一"

    # ── "Always match" for filter/required-only groups ────────────

    def test_filter_only_group_always_matches(self):
        """Group with only filter_words matches anything not filtered."""
        g = _make_group("过滤测试", filter_words=["垃圾"])
        assert match_title("正常标题", [g]) == "过滤测试"
        assert match_title("垃圾广告", [g]) is None  # filtered


# ═══════════════════════════════════════════════════════════════════════
# match_and_group — 批量分组
# ═══════════════════════════════════════════════════════════════════════

class TestMatchAndGroup:
    """Batch grouping tests."""

    def test_basic_grouping(self):
        groups = [
            _make_group("华为", words=["华为"]),
            _make_group("苹果", words=["苹果"]),
        ]
        items = [
            {"title": "华为发布新手机"},
            {"title": "苹果降价促销"},
            {"title": "华为鸿蒙系统发布"},
        ]
        result = match_and_group(items, groups)
        assert len(result["华为"]) == 2
        assert len(result["苹果"]) == 1

    def test_unmatched_items(self):
        groups = [_make_group("华为", words=["华为"])]
        items = [
            {"title": "华为发布新手机"},
            {"title": "三星发布新手机"},
        ]
        result = match_and_group(items, groups)
        assert len(result["华为"]) == 1
        assert len(result["__unmatched__"]) == 1

    def test_per_group_limit(self):
        g = _new_group("科技")
        g["words"] = ["科技"]
        g["max_count"] = 2
        items = [
            {"title": f"科技新闻 {i}"} for i in range(5)
        ]
        result = match_and_group(items, [g])
        assert len(result["科技"]) == 2
        assert len(result["__unmatched__"]) == 3

    def test_global_limit(self):
        g = _new_group("科技")
        g["words"] = ["科技"]
        items = [
            {"title": f"科技新闻 {i}"} for i in range(5)
        ]
        result = match_and_group(items, [g], max_per_group=3)
        assert len(result["科技"]) == 3
        assert len(result["__unmatched__"]) == 2

    def test_per_group_limit_overrides_global(self):
        g = _new_group("科技")
        g["words"] = ["科技"]
        g["max_count"] = 2
        items = [
            {"title": f"科技新闻 {i}"} for i in range(5)
        ]
        result = match_and_group(items, [g], max_per_group=10)
        assert len(result["科技"]) == 2  # per-group @2 wins

    def test_global_filter_in_match_and_group(self):
        g = _new_group("华为")
        g["words"] = ["华为"]
        items = [
            {"title": "震惊！华为发布新手机"},
            {"title": "华为财报公布"},
        ]
        result = match_and_group(items, [g], global_filters=["震惊"])
        assert len(result["华为"]) == 1  # only the non-震惊 one

    def test_no_items_returns_empty(self):
        groups = [_make_group("华为", words=["华为"])]
        result = match_and_group([], groups)
        assert result == {}  # all empty groups removed

    def test_empty_title_item(self):
        groups = [_make_group("华为", words=["华为"])]
        items = [{"title": ""}]
        result = match_and_group(items, groups)
        assert len(result["__unmatched__"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# Helpers for match_title / match_and_group tests
# ═══════════════════════════════════════════════════════════════════════

def _make_group(name: str, words=None, regexes=None,
                filter_words=None, required_words=None, max_count=0):
    g = _new_group(name)
    if words:
        g["words"] = words
    if regexes:
        g["regexes"] = [re.compile(r, re.IGNORECASE) for r in regexes]
    if filter_words:
        g["filter_words"] = filter_words
    if required_words:
        g["required_words"] = required_words
    g["max_count"] = max_count
    return g
