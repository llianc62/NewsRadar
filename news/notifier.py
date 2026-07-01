# coding=utf-8
"""Email report generation and SMTP notification.

HTML rendering uses Jinja2 templates from ``web/templates/notifier/``.

SMTP server is auto-detected from the ``from_addr`` email domain when
not explicitly configured.  See :data:`SMTP_PRESETS` for the built-in
provider table.
"""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytz
from jinja2 import Environment, BaseLoader

from storage.s3 import S3Client

# ── Jinja2 template setup ────────────────────────────────────────────
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "web" / "templates" / "notifier"

_env = Environment(
    loader=BaseLoader(),
    autoescape=False,  # Email HTML — we trust the template author
)


def load_template(template_name: str) -> str:
    """Read a template file from ``web/templates/notifier/``."""
    path = _TEMPLATES_DIR / template_name
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_template(template_name: str, **context: Any) -> str:
    """Load a template and render with Jinja2."""
    raw = load_template(template_name)
    template = _env.from_string(raw)
    return template.render(**context)


def build_html_report(
    grouped_items: Dict[str, List[Dict]],
    date: str,
    time_str: str,
    total_count: int,
) -> str:
    """Render the email report from the ``email_report.html`` Jinja2 template.

    Items not matching any keyword group (``__unmatched__``) are excluded.

    Args:
        grouped_items: {group_name: [item_dict, ...]} from match_and_group()
        date: YYYY-MM-DD
        time_str: HH:MM
        total_count: Total items in report

    Returns:
        Rendered HTML string.
    """
    from news.constants import TIER_LABELS, TIER_COLORS, TIER_BG

    matched = {k: v for k, v in grouped_items.items() if k != "__unmatched__"}
    return render_template(
        "email_report.html",
        date=date,
        time_str=time_str,
        total_count=total_count,
        grouped_items=matched,
        TIER_LABELS=TIER_LABELS,
        TIER_COLORS=TIER_COLORS,
        TIER_BG=TIER_BG,
    )


def save_html_report(html: str, data_dir: str, date: str, time_filename: str) -> Path:
    """Save rendered HTML report to ``{data_dir}/html/{date}/{time_filename}.html``.

    Args:
        html: Rendered HTML content.
        data_dir: Base data directory (e.g. ``"output"``).
        date: YYYY-MM-DD subdirectory.
        time_filename: Time part for filename (e.g. ``"08-30-00"``).

    Returns:
        Path to the saved file.
    """
    out_dir = Path(data_dir) / "html" / date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{time_filename}.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"[Notifier] Saved {out_path}")
    return out_path


# ── SMTP auto-detection ───────────────────────────────────────────────

SMTP_PRESETS: dict[str, tuple[str, int]] = {
    # 腾讯系
    "qq.com":            ("smtp.qq.com", 587),
    "foxmail.com":       ("smtp.qq.com", 587),
    # 网易系
    "163.com":           ("smtp.163.com", 465),
    "126.com":           ("smtp.126.com", 465),
    "yeah.net":          ("smtp.yeah.net", 465),
    # 国际
    "gmail.com":         ("smtp.gmail.com", 587),
    "outlook.com":       ("smtp-mail.outlook.com", 587),
    "hotmail.com":       ("smtp-mail.outlook.com", 587),
    "live.com":          ("smtp-mail.outlook.com", 587),
    "icloud.com":        ("smtp.mail.me.com", 587),
    "zoho.com":          ("smtp.zoho.com", 587),
    # 新浪 / 搜狐 / 阿里
    "sina.com":          ("smtp.sina.com", 587),
    "sohu.com":          ("smtp.sohu.com", 465),
    "aliyun.com":        ("smtp.aliyun.com", 465),
}

_DEFAULT_SMTP_SERVER = "smtp.qq.com"
_DEFAULT_SMTP_PORT = 587


def resolve_smtp_config(
    from_addr: str,
    smtp_server: str = "",
    smtp_port: int = 0,
) -> tuple[str, int]:
    """Resolve SMTP server and port.

    When *from_addr* is a recognised provider, use the preset values.
    Explicit *smtp_server* / *smtp_port* (from config) always take
    precedence.

    Returns:
        ``(server, port)`` tuple — guaranteed to be non-empty.
    """
    # 1. Explicit config wins
    if smtp_server and smtp_port:
        return smtp_server, smtp_port

    # 2. Auto-detect from from_addr domain
    domain = from_addr.rsplit("@", 1)[-1].strip().lower()
    preset = SMTP_PRESETS.get(domain)
    if preset is not None:
        return preset

    # 3. Partial override OR unknown domain → safe defaults
    return (
        smtp_server or _DEFAULT_SMTP_SERVER,
        smtp_port or _DEFAULT_SMTP_PORT,
    )


def send_email(
    html_content: str,
    smtp_server: str,
    smtp_port: int,
    from_addr: str,
    to_addr: str,
    password: str,
    subject: Optional[str] = None,
) -> bool:
    """Send HTML email via SMTP.

    Returns:
        True on success, False on failure.
    """
    if not subject:
        subject = "\U0001f4f0 新闻速报"

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr(("NewsRadar", from_addr))
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)

        # Plain text fallback
        text_part = MIMEText(
            "请使用支持 HTML 的邮件客户端查看。",
            "plain",
            "utf-8",
        )
        msg.attach(text_part)

        # HTML part
        html_part = MIMEText(html_content, "html", "utf-8")
        msg.attach(html_part)

        # Connect and send
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.starttls()

        server.login(from_addr, password)
        recipients = [addr.strip() for addr in to_addr.split(",") if addr.strip()]
        server.sendmail(from_addr, recipients, msg.as_string())
        server.quit()

        print(f"[Email] Sent to {len(recipients)} recipient(s)")
        return True

    except Exception as e:
        print(f"[Email] Failed: {e}")
        return False


def _iso_to_db_format(iso_str: str | None, target_tz: str = "Asia/Shanghai") -> str | None:
    """Convert ISO 8601 string to ``YYYY-MM-DD HH:MM:SS`` for SQLite comparison.

    The input is treated as UTC (or a timezone-aware string). It is converted
    to *target_tz* before formatting because SQLite ``created_at`` values are
    stored in the configured timezone (default ``Asia/Shanghai``).

    Returns ``None`` if *iso_str* is empty or unparseable -- callers should
    treat ``None`` as "no filter".
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(pytz.timezone(target_tz))
        else:
            # Naive datetime — assume already in target_tz (no conversion needed)
            pass
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        print(f"[Notifier] Failed to parse time: {iso_str!r}, ignoring filter")
        return None


# ── Rules (filters applied sequentially before keyword matching) ─────


def rules_blacklist(items: list[dict], black_list: list[str]) -> list[dict]:
    """按 source_id 黑名单过滤新闻。

    Args:
        items: 新闻 dict 列表，每条需有 ``source_id`` 字段。
        black_list: 要排除的 source_id 列表。

    Returns:
        过滤后的新闻列表。
    """
    if not black_list:
        return items
    before = len(items)
    items = [it for it in items if it.get("source_id") not in black_list]
    skipped = before - len(items)
    if skipped:
        print(
            f"[Notifier] rules_blacklist: filtered {skipped} items "
            f"({len(items)} remaining)"
        )
    return items


def run_notifier(
    config: dict,
    dry_run: bool = False,
    start_time: str | None = None,
    end_time: str | None = None,
) -> None:
    """Run the full notification pipeline (render, save, optionally send).

    1. Download daily SQLite DB from S3 (CI only)
    2. Query all items
    3. Match keywords and group
    4. Build HTML report
    5. Save to ``{data_dir}/html/{date}/{time}.html``
    6. Send email (unless *dry_run*)

    Args:
        config: Full configuration dict.
        dry_run: If True, save the HTML report but skip sending email.
    """
    import os

    from news.keywords import load_frequency_words, match_and_group
    from storage.sqlite import Sqlite
    from utils import format_date_today, format_time_now, get_configured_time, DEFAULT_TIMEZONE

    timezone = config.get("app", {}).get("timezone", DEFAULT_TIMEZONE)
    date = format_date_today(timezone)
    time_str = format_time_now(timezone)
    time_filename = get_configured_time(timezone).strftime("%H-%M-%S")

    print(f"=== Notifier === {date} {time_str}")

    storage_config = config.get("storage", {})
    data_dir = storage_config.get("local", {}).get("data_path", "output")

    # ── Download daily DB from S3 ─────────────────────────────────
    # GitHub Actions runs are ephemeral — pull the snapshot first.
    s3 = S3Client.init_by_config(config["storage"]["cloud"])
    if not s3:
        raise ValueError(
            "notify requires S3 storage. "
            "Configure storage.cloud in config.yaml or set CLOUD_S3_* env vars."
        )
    db_path = Path(data_dir) / "db" / f"{date}.db"
    if s3.object_exists(f"db/{date}.db"):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if s3.download_file(f"db/{date}.db", db_path):
            print("[Notify] Restored DB from S3")
        else:
            print("[Notify] Failed to download DB from S3")

    db = Sqlite(data_dir=data_dir, timezone=timezone)
    db_start = _iso_to_db_format(start_time, timezone)
    db_end = _iso_to_db_format(end_time, timezone)
    rows = db.get_all(date, start_time=db_start, end_time=db_end)
    if not rows:
        print("No items to notify")
        db.cleanup()
        return

    # Convert rows to dicts
    items = [dict(row) for row in rows]
    print(f"Total items: {len(items)}")

    # ── Apply rules ───────────────────────────────────────────────
    black_list = config.get("notification", {}).get("black_list", [])
    items = rules_blacklist(items, black_list)

    # Load keywords and match
    freq_path = config.get("notification", {}).get(
        "frequency_words", "frequency_words.txt"
    )
    if not os.path.exists(freq_path):
        # Fall back to root-level file for backward compatibility
        freq_path = "frequency_words.txt"
    if os.path.exists(freq_path):
        word_groups, filter_words, global_filters = load_frequency_words(freq_path)
        max_per = config.get("notification", {}).get("keyword_limit_news", 0)
        grouped = match_and_group(items, word_groups, global_filters, max_per)
        print(f"Matched groups: {list(grouped.keys())}")
    else:
        grouped = {"全部新闻": items}

    # Build HTML report
    html = build_html_report(grouped, date, time_str, len(items))

    # Save to output directory
    save_html_report(html, data_dir, date, time_filename)

    # Send email (skip in dry-run mode)
    if dry_run:
        print("[Notifier] Dry run — skipping email send")
    else:
        email_config = config.get("notification", {}).get("email", {})
        from_addr = email_config.get("from_addr", "")
        to_addr = email_config.get("to_addr", "")
        password = email_config.get("password") or os.environ.get("EMAIL_PASSWORD", "")
        smtp_server, smtp_port = resolve_smtp_config(from_addr)

        if not all([from_addr, to_addr, password]):
            print("[Email] Missing config — skipping send")
        else:
            send_email(html, smtp_server, smtp_port, from_addr, to_addr, password)

    db.cleanup()
    print("=== Done ===")
