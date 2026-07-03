# coding=utf-8
"""JiebaAnalyzer — local offline analysis using jieba."""

import os
import re
import math
from typing import Dict, Optional

import jieba
import jieba.analyse

from news.analyzer.analyzer import Analyzer

# 情感词典路径
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")


class JiebaAnalyzer(Analyzer):
    """基于 jieba 的本地离线分析器。

    负责：
    - sentiment_score: 基于词典的情感分析
    - analyze_keywords: TF-IDF / TextRank 关键词提取

    热度分由基类 :class:`Analyzer` 提供通用实现。
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        # 情感词典惰性加载
        self._positive_dict: Optional[Dict[str, float]] = None
        self._negative_dict: Optional[Dict[str, float]] = None
        self._negation_set: Optional[set] = None
        self._degree_dict: Optional[Dict[str, float]] = None

    # ── Utility ──────────────────────────────────────────────────────

    def _load_dict(self, filepath: str) -> Dict[str, float]:
        """Load a word-weight dictionary file.

        Format: one entry per line — ``word  weight`` (space-separated).
        Lines starting with ``#`` are comments.
        """
        d = {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.rsplit(None, 1)  # word  weight
                    if len(parts) == 2:
                        try:
                            d[parts[0]] = float(parts[1])
                        except ValueError:
                            pass
        except FileNotFoundError:
            pass
        return d

    # ── Sentiment (Task 6 实现) ────────────────────────────────────

    def _ensure_dicts(self) -> None:
        """惰性加载情感词典。"""
        if self._positive_dict is not None:
            return
        self._positive_dict = self._load_dict(
            os.path.join(_DATA_DIR, "senti_positive.txt"))
        self._negative_dict = self._load_dict(
            os.path.join(_DATA_DIR, "senti_negative.txt"))
        self._degree_dict = self._load_dict(
            os.path.join(_DATA_DIR, "senti_degree.txt"))
        self._negation_set = set()
        neg_path = os.path.join(_DATA_DIR, "senti_negation.txt")
        try:
            with open(neg_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        self._negation_set.add(line)
        except FileNotFoundError:
            pass

    def _get_value(self, item, key, default=None):
        """获取 item 属性，兼容 dict 和 NewsItem。"""
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    def _set_value(self, item, key, value):
        """设置 item 属性，兼容 dict 和 NewsItem。"""
        if isinstance(item, dict):
            item[key] = value
        else:
            setattr(item, key, value)

    def _score_words(self, words: list) -> tuple:
        """遍历分词结果，返回 (pos_score, neg_score)。"""
        pos = 0.0
        neg = 0.0
        negation_active = 0  # 否定词作用窗口（剩余词数）
        degree_multiplier = 1.0

        for w in words:
            # 程度副词：修改当前乘数
            if w in self._degree_dict:
                degree_multiplier = self._degree_dict[w]
                continue

            # 否定词：翻转后续 3 词的极性
            if w in self._negation_set:
                negation_active = 3
                continue

            # 正面词
            if w in self._positive_dict:
                weight = self._positive_dict[w] * degree_multiplier
                if negation_active > 0:
                    neg += weight  # 否定 → 归入负面
                    negation_active -= 1
                else:
                    pos += weight

            # 负面词
            elif w in self._negative_dict:
                weight = self._negative_dict[w] * degree_multiplier
                if negation_active > 0:
                    pos += weight  # 否定 → 归入正面
                    negation_active -= 1
                else:
                    neg += weight

            # 窗口递减（非情感词也消耗窗口）
            elif negation_active > 0:
                negation_active -= 1

            # 重置乘数（每个词只用一次）
            degree_multiplier = 1.0

        return pos, neg

    def _to_sentiment_score(self, pos: float, neg: float) -> int:
        """将正负得分映射到 0-100。"""
        if pos + neg == 0:
            return 50  # 中性
        net = pos - neg
        scaled = math.tanh(net / 5.0) * 50.0  # -50 ~ +50
        return round(50 + scaled)  # 0-100

    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 sentiment_score。

        兼容 dict 和 NewsItem 两种输入。
        """
        self._ensure_dicts()

        for item in items:
            title = self._get_value(item, "title") or ""
            content = self._get_value(item, "content") or ""
            # content 含 markdown 语法，先清理
            content = self._clean_markdown_syntax(content)
            text = title + " " + content

            if not text.strip():
                self._set_value(item, "sentiment_score", 50)
                continue

            # jieba 分词
            words = jieba.lcut(text)

            # 逐词评分
            pos_score, neg_score = self._score_words(words)

            # 映射到 0-100
            self._set_value(item, "sentiment_score", self._to_sentiment_score(pos_score, neg_score))


    # ── Keyword extraction ─────────────────────────────────────────

    def analyze_keywords(self, items: list, topk: int = 5) -> None:
        """从正文提取关键词，原地修改 item['tags']。

        对每个 item 提取 content → TF-IDF（优先）→ TextRank（兜底）。
        若 parser 阶段已提取到 tags 则跳过，保留原始元数据标签。
        兼容 dict 和 NewsItem 两种输入。
        """
        for item in items:
            if self._get_value(item, "tags"):
                continue
            content = self._get_value(item, "content") or ""
            self._set_value(item, "tags", self._extract_keywords(content, topk=topk))

    def _extract_keywords(self, content: str, topk: int = 5) -> list[str]:
        """从单篇 Markdown 正文提取关键词。

        默认使用 jieba 内置 IDF 做 TF-IDF 提取，失败则回退到 TextRank。
        """
        text = self._clean_markdown_syntax(content)
        if len(text) < 50:
            return []
        
        # ── TF-IDF (优先) ──────────────────────────────────────────
        try:
            keywords = jieba.analyse.tfidf(
                text,
                topK=topk,
                withWeight=False,
                allowPOS=('ns', 'nr', 'nt', 'nz'),
            )
            if keywords:
                return keywords
        except Exception:
            pass

        # ── TextRank (兜底) ─────────────────────────────────────────
        return self._analyze_keywords_textrank(content, topk=topk)

    def _analyze_keywords_textrank(self, content: str, topk: int = 5) -> list[str]:
        """从 Markdown 正文提取关键词，使用 jieba TextRank + 专有名词过滤。

        Args:
            content: Markdown 格式的 article body。
            topk: 最多返回的关键词数量。

        Returns:
            关键词列表，或空列表当正文过短或无法提取。
        """
        text = self._clean_markdown_syntax(content)
        if len(text) < 50:
            return []

        # 只保留专有名词类：地名/人名/机构名/其他专名
        keywords = jieba.analyse.textrank(
            text,
            topK=topk,
            withWeight=False,
            allowPOS=('ns', 'nr', 'nt', 'nz'),
        )
        return keywords

    def _clean_markdown_syntax(self, content: str) -> str:
        """Remove Markdown syntax noise for cleaner NLP input."""
        text = re.sub(r'!\[.*?\]\(.*?\)', '', content)          # 图片
        text = re.sub(r'\[([^\]]*)\]\(.*?\)', r'\1', text)      # 链接保留文字
        text = re.sub(r'[#*>`|~\-_]', ' ', text)                # 格式标记
        return re.sub(r'\s+', ' ', text).strip()
