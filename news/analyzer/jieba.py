# coding=utf-8
"""JiebaAnalyzer — local offline analysis using jieba."""

import math
import os
import re
from typing import Any, Dict, List, Optional

from news.analyzer.analyzer import Analyzer

# 词典文件默认路径
_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data")

# IDF 语料路径 — 从数据库文章构建，供 TF-IDF 压低通用词使用
_IDF_PATH = os.path.join(_DATA_DIR, "jieba_idf.txt")


def clean_markdown_syntax(content: str) -> str:
    """Remove Markdown syntax noise for cleaner NLP input."""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', content)          # 图片
    text = re.sub(r'\[([^\]]*)\]\(.*?\)', r'\1', text)      # 链接保留文字
    text = re.sub(r'[#*>`|~\-_]', ' ', text)                # 格式标记
    return re.sub(r'\s+', ' ', text).strip()


def extract_keywords_textrank(content: str, topk: int = 5) -> list[str]:
    """从 Markdown 正文提取关键词，使用 jieba TextRank + 专有名词过滤。

    Args:
        content: Markdown 格式的 article body。
        topk: 最多返回的关键词数量。

    Returns:
        关键词列表，或空列表当正文过短或无法提取。
    """
    text = clean_markdown_syntax(content)
    if len(text) < 50:
        return []

    try:
        import jieba.analyse
    except ImportError:
        return []

    # 只保留专有名词类：地名/人名/机构名/其他专名
    keywords = jieba.analyse.textrank(
        text,
        topK=topk,
        withWeight=False,
        allowPOS=('ns', 'nr', 'nt', 'nz'),
    )
    return keywords


def _load_dict(filepath: str) -> Dict[str, float]:
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


class JiebaAnalyzer(Analyzer):
    """基于 jieba 的本地离线分析器。

    负责：
    - heat_score: 热度分计算（从 PostgreSQL 迁移）
    - sentiment_score: 基于词典的情感分析
    """

    def __init__(self, config: dict, db=None):
        super().__init__(config, db)
        # 情感词典惰性加载
        self._positive_dict: Optional[Dict[str, float]] = None
        self._negative_dict: Optional[Dict[str, float]] = None
        self._negation_set: Optional[set] = None
        self._degree_dict: Optional[Dict[str, float]] = None

    # ── Heat score ─────────────────────────────────────────────────

    @staticmethod
    def _calc_heat_score(
        prev_heat: Optional[int],
        prev_ranks: list,       # [[7,20], [5,20]]
        new_ranks_entry: list,  # [rank, total] from current round
    ) -> int:
        """Calculate heat score, returns 0-100."""
        new_rank, new_total = new_ranks_entry
        if not prev_ranks or prev_heat is None:
            # First appearance: percentile
            return round(max(0, min(100, (1 - new_rank / new_total) * 100)))

        # Still on the list: incremental adjustment
        last_r, last_t = prev_ranks[-1]
        last_pct = (1 - last_r / last_t) * 100
        new_pct = (1 - new_rank / new_total) * 100
        delta = new_pct - last_pct  # percentage-point difference

        return round(max(0, min(100, prev_heat + delta * 0.3)))

    def analyze_heat(self, source_id: str, items: list, db_map: dict) -> None:
        """Process heat score for hotlist items of one source.

        db_map 格式: {url: {"heat_score": int, "ranks": [[int,int],...]}}

        items: list[dict] — 与 Crawler.fetch_all 传递的 item dict 一致。
        """
        valid_items = [it for it in items if it.get("ranks")]

        # ① Compare sets
        this_urls = {item["url"] for item in valid_items if item.get("url")}
        db_urls = set(db_map.keys())

        new_urls = this_urls - db_urls
        existing_urls = this_urls & db_urls
        dropped_urls = db_urls - this_urls

        # ② First appearance — percentile
        for item in valid_items:
            if item["url"] in new_urls:
                r, t = item["ranks"][0]
                item["heat_score"] = round(
                    max(0, min(100, (1 - r / t) * 100))
                )
                item["ranks"] = [[r, t]]

        # ③ Still on list — delta adjustment
        for item in valid_items:
            if item["url"] in existing_urls:
                prev = db_map[item["url"]]
                item["heat_score"] = self._calc_heat_score(
                    prev_heat=prev["heat_score"],
                    prev_ranks=prev["ranks"],
                    new_ranks_entry=item["ranks"][0],
                )
                item["ranks"] = (prev["ranks"] or []) + [item["ranks"][0]]

        # ④ Dropped from list — ×0.7 decay (requires DB write)
        if dropped_urls and self._db is not None:
            with self._db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE news_articles
                           SET heat_score = CAST(
                               ROUND(GREATEST(0, LEAST(100,
                                   COALESCE(heat_score, 0) * 0.7
                               ))) AS INTEGER
                           )
                           WHERE source_id = %s
                             AND source_type = 'hotlist'
                             AND url = ANY(%s)""",
                        (source_id, list(dropped_urls)),
                    )
            print(
                f"[Analyzer] Heat decay: {len(dropped_urls)} URLs dropped"
                f" from {source_id}"
            )

    # ── Sentiment (Task 6 实现) ────────────────────────────────────

    def _ensure_dicts(self) -> None:
        """惰性加载情感词典。"""
        if self._positive_dict is not None:
            return
        self._positive_dict = _load_dict(
            os.path.join(_DATA_DIR, "senti_positive.txt"))
        self._negative_dict = _load_dict(
            os.path.join(_DATA_DIR, "senti_negative.txt"))
        self._degree_dict = _load_dict(
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

    @staticmethod
    def _get_value(item, key, default=None):
        """获取 item 属性，兼容 dict 和 NewsItem。"""
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _set_value(item, key, value):
        """设置 item 属性，兼容 dict 和 NewsItem。"""
        if isinstance(item, dict):
            item[key] = value
        else:
            setattr(item, key, value)

    def analyze_sentiment(self, items: list) -> None:
        """计算情感分。原地修改 sentiment_score。

        兼容 dict 和 NewsItem 两种输入。
        """
        self._ensure_dicts()

        import jieba

        for item in items:
            title = self._get_value(item, "title") or ""
            content = self._get_value(item, "content") or ""
            # content 含 markdown 语法，先清理
            content = clean_markdown_syntax(content)
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

    @staticmethod
    def _to_sentiment_score(pos: float, neg: float) -> int:
        """将正负得分映射到 0-100。"""
        if pos + neg == 0:
            return 50  # 中性
        net = pos - neg
        scaled = math.tanh(net / 5.0) * 50.0  # -50 ~ +50
        return round(50 + scaled)  # 0-100

    # ── Keyword extraction ─────────────────────────────────────────

    def extract_keywords(self, content: str, topk: int = 5) -> list[str]:
        """从 Markdown 正文提取关键词，TF-IDF 优先，TextRank 兜底。

        尝试加载数据库构建的自定义 IDF 语料，使 TF-IDF 能自动
        压低"在大多数文章都出现"的通用词（如 公司/企业/项目）。
        如果 IDF 语料不可用，回退到 TextRank + 专有名词过滤。
        """
        text = clean_markdown_syntax(content)
        if len(text) < 50:
            return []

        try:
            import jieba.analyse
        except ImportError:
            return []

        # ── TF-IDF (优先) ──────────────────────────────────────────
        idf_path = self._ensure_idf_corpus()
        if idf_path is not None:
            try:
                default_idf = jieba.analyse.idf_path
                jieba.analyse.set_idf_path(idf_path)
                keywords = jieba.analyse.tfidf(
                    text,
                    topK=topk,
                    withWeight=False,
                    allowPOS=('ns', 'nr', 'nt', 'nz'),
                )
                if default_idf:
                    jieba.analyse.set_idf_path(default_idf)
                if keywords:
                    return keywords
            except Exception:
                pass

        # ── TextRank (兜底) ─────────────────────────────────────────
        return extract_keywords_textrank(content, topk=topk)

    def _ensure_idf_corpus(self) -> Optional[str]:
        """确保自定义 IDF 语料文件存在，不存在则从数据库构建。

        Returns:
            IDF 文件路径，或 None 当数据库无可用文章或构建失败。
        """
        if os.path.exists(_IDF_PATH):
            return _IDF_PATH

        if self._db is None:
            return None

        # ── 从数据库收集文章正文 ──────────────────────────────────
        contents: List[str] = []
        try:
            with self._db.get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT content FROM news_articles "
                        "WHERE content IS NOT NULL AND content != '' "
                        "ORDER BY created_at DESC LIMIT 2000"
                    )
                    rows = cur.fetchall()
        except Exception as e:
            print(f"[Analyzer] IDF corpus query failed: {e}")
            return None

        for (body,) in rows:
            cleaned = clean_markdown_syntax(body)
            if len(cleaned) >= 50:
                contents.append(cleaned)

        if len(contents) < 10:
            return None

        # ── 分词 + 文档频率统计 ───────────────────────────────────
        try:
            import jieba
        except ImportError:
            return None

        df: Dict[str, int] = {}
        for text in contents:
            words = set(jieba.cut(text))
            for w in words:
                if len(w) < 2:
                    continue
                df[w] = df.get(w, 0) + 1

        total_docs = len(contents)

        # ── 写入 IDF 文件 ─────────────────────────────────────────
        os.makedirs(os.path.dirname(_IDF_PATH), exist_ok=True)
        try:
            with open(_IDF_PATH, "w", encoding="utf-8") as f:
                for word, count in df.items():
                    idf = math.log(total_docs / count)
                    f.write(f"{word} {idf:.6f}\n")
            print(
                f"[Analyzer] Built IDF corpus: {len(df)} words "
                f"from {total_docs} articles → {_IDF_PATH}"
            )
            return _IDF_PATH
        except OSError as e:
            print(f"[Analyzer] Failed to write IDF corpus: {e}")
            return None
