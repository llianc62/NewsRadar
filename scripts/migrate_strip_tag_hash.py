#!/usr/bin/env python3
"""迁移脚本：去掉数据库中所有 tags 的 "#" 前缀。

用法:
    python scripts/migrate_strip_tag_hash.py [--dry-run]
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import psycopg2.extras


def get_config():
    from config import load_config
    return load_config()


def migrate_postgresql(dry_run: bool = False):
    config = get_config()
    pg_config = config["postgresql"]

    conn = psycopg2.connect(
        host=pg_config["host"],
        port=pg_config["port"],
        database=pg_config["database"],
        user=pg_config["user"],
        password=pg_config["password"],
    )
    conn.autocommit = False

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # 1. 查看有多少条记录需要处理
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM news_articles
            WHERE tags IS NOT NULL
              AND array_length(tags, 1) > 0
        """)
        total = cur.fetchone()["cnt"]
        print(f"共 {total} 条记录有 tags")

        # 2. 查看有哪些带 # 的 tag
        cur.execute("""
            WITH all_tags AS (
                SELECT unnest(tags) AS tag
                FROM news_articles
                WHERE tags IS NOT NULL
            )
            SELECT DISTINCT tag, COUNT(*) as cnt
            FROM all_tags
            WHERE tag LIKE '#%'
            GROUP BY tag
            ORDER BY cnt DESC
            LIMIT 30
        """)
        hash_tags = cur.fetchall()
        print(f"\n带 '#' 前缀的 tag 示例 (前30):")
        for row in hash_tags:
            print(f"  {row['tag']} ({row['cnt']}次)")

        # 3. 统计需要更新的记录数
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM news_articles
            WHERE EXISTS (
                SELECT 1 FROM unnest(tags) AS t WHERE t LIKE '#%'
            )
        """)
        to_update = cur.fetchone()["cnt"]
        print(f"\n需要更新的记录数: {to_update}")

        if to_update == 0:
            print("无需更新，所有 tags 已经干净。")
            return

        if dry_run:
            # 预览更新后的效果
            cur.execute("""
                WITH original AS (
                    SELECT unnest(tags) AS old_tag
                    FROM news_articles
                    WHERE EXISTS (
                        SELECT 1 FROM unnest(tags) AS t WHERE t LIKE '#%'
                    )
                    LIMIT 10
                )
                SELECT DISTINCT old_tag, ltrim(old_tag, '#') AS new_tag
                FROM original
                WHERE old_tag LIKE '#%'
                LIMIT 20
            """)
            preview = cur.fetchall()
            print("\n[Dry Run] 预览变更 (前20):")
            for row in preview:
                print(f"  {row['old_tag']} → {row['new_tag']}")
            print(f"\n[Dry Run] 共 {to_update} 条记录需要更新。使用 --execute 执行。")
            return

        # 4. 执行更新
        print("\n执行迁移...")
        cur.execute("""
            UPDATE news_articles
            SET tags = (
                SELECT array_agg(ltrim(t, '#'))
                FROM unnest(tags) AS t
            )
            WHERE EXISTS (
                SELECT 1 FROM unnest(tags) AS t WHERE t LIKE '#%'
            )
        """)
        conn.commit()
        print(f"已更新 {cur.rowcount} 条记录")

        # 5. 验证
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM news_articles
            WHERE EXISTS (
                SELECT 1 FROM unnest(tags) AS t WHERE t LIKE '#%'
            )
        """)
        remaining = cur.fetchone()["cnt"]
        if remaining == 0:
            print("验证通过：所有 tags 的 # 前缀已清除。")
        else:
            print(f"验证警告：仍有 {remaining} 条记录带 # 前缀！")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="去掉数据库中所有 tags 的 \"#\" 前缀"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="预览变更，不实际执行",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="确认执行迁移",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.execute:
        print("请指定 --dry-run 预览或 --execute 执行迁移。")
        sys.exit(1)

    migrate_postgresql(dry_run=args.dry_run)
