# coding=utf-8
"""HTML report generation and email notification.

The HTML layout and styles are defined in ``report_template.html``.
This module renders the template with actual news data.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Dict, List, Optional

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "report_template.html")

# Tier display config
TIER_LABELS = {1: "T1·官媒", 2: "T2·主流", 3: "T3·垂直", 4: "T4·资讯"}
TIER_COLORS = {1: "#059669", 2: "#2563eb", 3: "#d97706", 4: "#6b7280"}
TIER_BG = {1: "#ecfdf5", 2: "#eff6ff", 3: "#fffbeb", 4: "#f3f4f6"}


def _render_news_item(item: Dict, index: int) -> str:
    """Render a single news item as an HTML snippet.

    Args:
        item: News item dict with fields: title, source_name, source_type,
              url, rank, tier.
        index: 1-based item number within the group.

    Returns:
        HTML string for the news item div.
    """
    title = item.get("title", "")
    source = item.get("source_name", "")
    source_type = item.get("source_type", "hotlist")
    url = item.get("url", "")
    rank = item.get("rank", "")
    tier = item.get("tier", 4)

    tier_color = TIER_COLORS.get(tier, "#6b7280")
    tier_bg_color = TIER_BG.get(tier, "#f3f4f6")
    tier_label = TIER_LABELS.get(tier, "T4")

    # Rank / type badge
    if source_type == "rss":
        type_badge = '<span class="rank-num" style="background:#059669;">RSS</span>'
    elif rank:
        rank_int = rank if isinstance(rank, int) else (int(rank) if str(rank).isdigit() else 0)
        if rank_int <= 3:
            rank_class = "top"
        elif rank_int <= 10:
            rank_class = "high"
        else:
            rank_class = ""
        type_badge = f'<span class="rank-num {rank_class}">#{rank}</span>'
    else:
        type_badge = ""

    # HTML-escape title and source
    escaped_title = (
        title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    escaped_source = (
        source.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

    title_link = (
        f'<a href="{url}" target="_blank" class="news-link">{escaped_title}</a>'
        if url
        else escaped_title
    )

    return f"""<div class="news-item">
    <div class="news-number">{index}</div>
    <div class="news-content">
        <div class="news-header">
            {type_badge}
            <span class="tier-badge" style="background:{tier_bg_color};color:{tier_color};">{tier_label}</span>
            <span class="source-name">{escaped_source}</span>
        </div>
        <div class="news-title">{title_link}</div>
    </div>
</div>"""


def _render_content(grouped_items: Dict[str, List[Dict]]) -> str:
    """Render all grouped news items into the {{CONTENT}} HTML block.

    Args:
        grouped_items: {group_name: [item_dict, ...]} from match_and_group().

    Returns:
        HTML string for the content area.
    """
    parts = []
    total_groups = len(grouped_items)
    group_index = 0

    for group_name, items in grouped_items.items():
        if not items:
            continue
        label = group_name
        count = len(items)

        items_html = "\n".join(
            _render_news_item(item, j) for j, item in enumerate(items, 1)
        )

        parts.append(f"""<div class="word-group">
    <div class="word-header">
        <div class="word-info">
            <div class="word-name">{label}</div>
            <div class="word-count">{count} 条</div>
        </div>
        <div class="word-index">{group_index + 1}/{total_groups}</div>
    </div>
    {items_html}
</div>""")
        group_index += 1

    return "\n".join(parts)


def load_template(path: str = TEMPLATE_PATH) -> str:
    """Load the HTML report template from disk."""
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_html_report(
    grouped_items: Dict[str, List[Dict]],
    date: str,
    time_str: str,
    total_count: int,
    *,
    template_path: str = TEMPLATE_PATH,
) -> str:
    """Render the HTML report by filling the template with live data.

    Args:
        grouped_items: {group_name: [item_dict, ...]} from match_and_group().
        date: YYYY-MM-DD.
        time_str: HH:MM.
        total_count: Total items in the report.
        template_path: Path to the HTML template file (defaults to
            ``report_template.html`` next to this module).

    Returns:
        Complete HTML string ready for email or file output.
    """
    template = load_template(template_path)
    content_html = _render_content(grouped_items)

    return (
        template.replace("{{DATE}}", date)
        .replace("{{TIME}}", time_str)
        .replace("{{TOTAL_COUNT}}", str(total_count))
        .replace("{{GROUP_COUNT}}", str(len(grouped_items)))
        .replace("{{CONTENT}}", content_html)
    )


def save_html_report(
    html_content: str,
    date: str,
    time_str: str,
    *,
    data_dir: str = "output",
) -> str:
    """Save the HTML report to a local file before sending.

    Args:
        html_content: The complete HTML report string.
        date: YYYY-MM-DD.
        time_str: HH:MM (colon replaced with dash in filename).
        data_dir: Base directory for output files.

    Returns:
        Absolute path to the saved report file.
    """
    os.makedirs(data_dir, exist_ok=True)
    safe_time = time_str.replace(":", "-")
    filename = f"report_{date}_{safe_time}.html"
    filepath = os.path.join(data_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"[Report] Saved to {filepath}")
    return os.path.abspath(filepath)


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
        msg["From"] = formataddr(("NewsNow Crawler", from_addr))
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
