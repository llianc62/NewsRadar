#!/usr/bin/env python3
"""迁移脚本：将 T1 主流财经源的 tier 从 2 更新为 1。

涉及 source_id:
  - wallstreetcn-hot / wallstreetcn-news (华尔街见闻)
  - cls-hot / cls-depth (财联社)
  - fastbull-news (法布财经)

用法:
    python scripts/migrate_tier_t1.py --dry-run
    python scripts/migrate_tier_t1.py --execute
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
import psycopg2.extras

# 需要将 tier 从 2 更新为 1 的 source_id 列表
T1_SOURCE_IDS = [
    "wallstreetcn-hot",
    "wallstreetcn-news",
    "cls-hot",
    "cls-depth",
    "fastbull-news",
]


def get_config():
    from config.loader import load_config
    return load_config()


def migrate_tier_t1(dry_run: bool = False):
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
        # 1. 查看当前 tier 分布
        cur.execute(
            """SELECT source_id, tier, COUNT(*) as cnt
               FROM news_articles
               WHERE source_id = ANY(%s)
               GROUP BY source_id, tier
               ORDER BY source_id, tier""",
            (T1_SOURCE_IDS,),
        )
        current = cur.fetchall()
        if not current:
            print("数据库中未找到这些 source_id 的记录。")
            conn.close()
            return

        print("当前 tier 分布:")
        for row in current:
            label = "✅ 已为 1" if row["tier"] == 1 else "⚠️  待更新"
            print(f"  {row['source_id']}: tier={row['tier']}, {row['cnt']} 条  {label}")

        # 2. 统计需要更新的记录数
        cur.execute(
            """SELECT source_id, COUNT(*) as cnt
               FROM news_articles
               WHERE source_id = ANY(%s) AND tier != 1
               GROUP BY source_id
               ORDER BY source_id""",
            (T1_SOURCE_IDS,),
        )
        to_update = cur.fetchall()
        total_update = sum(row["cnt"] for row in to_update)

        if total_update == 0:
            print("\n所有记录的 tier 已为 1，无需更新。")
            conn.close()
            return

        print(f"\n待更新记录数: {total_update}")
        for row in to_update:
            print(f"  {row['source_id']}: {row['cnt']} 条")

        if dry_run:
            print(f"\n[Dry Run] 共 {total_update} 条记录需要更新。使用 --execute 执行。")
            conn.close()
            return

        # 3. 执行更新
        print("\n执行迁移...")
        cur.execute(
            """UPDATE news_articles
               SET tier = 1, updated_at = NOW()
               WHERE source_id = ANY(%s) AND tier != 1""",
            (T1_SOURCE_IDS,),
        )
        conn.commit()
        print(f"已更新 {cur.rowcount} 条记录")

        # 4. 验证
        cur.execute(
            """SELECT source_id, tier, COUNT(*) as cnt
               FROM news_articles
               WHERE source_id = ANY(%s)
               GROUP BY source_id, tier
               ORDER BY source_id, tier""",
            (T1_SOURCE_IDS,),
        )
        after = cur.fetchall()
        print("\n更新后 tier 分布:")
        all_t1 = True
        for row in after:
            status = "✅" if row["tier"] == 1 else "❌"
            if row["tier"] != 1:
                all_t1 = False
            print(f"  {row['source_id']}: tier={row['tier']}, {row['cnt']} 条  {status}")

        if all_t1:
            print("\n验证通过：所有 T1 源的 tier 已更新为 1。")
        else:
            print("\n验证警告：仍有记录未更新为 tier=1！")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="将 T1 主流财经源的数据库记录从 tier=2 更新为 tier=1"
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

    migrate_tier_t1(dry_run=args.dry_run)
