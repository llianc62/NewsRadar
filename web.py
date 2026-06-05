"""NewsRadar Web Frontend — FastAPI + Jinja2 SSR."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# Jinja2 environment
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),
)

# Lucide icon SVG map (16x16, stroke-width 2, stroke-linecap round, stroke-linejoin round)
ICONS = {
    "chart-column": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 20V8"/><path d="M10 20V4"/><path d="M14 20V12"/><path d="M18 20V16"/></svg>',
    "flame": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a3.5 3.5 0 0 0 2.5 2.5z"/></svg>',
    "briefcase-business": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12h.01"/><path d="M16 6V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2"/><path d="M22 13a19 19 0 0 0-20 0"/><rect x="2" y="6" width="20" height="14" rx="2"/><rect x="6" y="12" width="12" height="6"/></svg>',
    "trending-up": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
    "file-text": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>',
    "clock": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    "sliders-horizontal": '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="2" y1="14" x2="6" y2="14"/><line x1="10" y1="8" x2="14" y2="8"/><line x1="18" y1="16" x2="22" y2="16"/></svg>',
    "newspaper": '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="9" x2="15" y2="9"/><line x1="9" y1="13" x2="15" y2="13"/><line x1="9" y1="17" x2="12" y2="17"/></svg>',
    "star": '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
    "trending-up-lg": '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 7 13.5 15.5 8.5 10.5 2 17"/><polyline points="16 7 22 7 22 13"/></svg>',
}
env.globals["icon_svg"] = lambda name: ICONS.get(name, "")
env.globals["len"] = len


def render_template(name: str, **context) -> str:
    """Render a Jinja2 template."""
    template = env.get_template(name)
    return template.render(**context)


app = FastAPI(title="NewsRadar", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ===== Routes =====

@app.get("/", response_class=HTMLResponse)
async def market_overview(request: Request):
    """Market overview page."""
    index_cards = [
        {"name": "上证指数", "value": "3,258.16", "change": 1.23},
        {"name": "深证成指", "value": "11,432.07", "change": 0.87},
        {"name": "创业板指", "value": "2,358.42", "change": -0.45},
        {"name": "恒生科技", "value": "4,521.33", "change": 2.15},
        {"name": "纳斯达克", "value": "19,856.27", "change": 0.62},
        {"name": "标普500", "value": "5,934.11", "change": 0.38},
    ]
    hot_stocks = [
        {"name": "贵州茅台", "change": 2.34},
        {"name": "宁德时代", "change": 4.12},
        {"name": "腾讯控股", "change": 3.15},
        {"name": "比亚迪", "change": -1.87},
        {"name": "中芯国际", "change": 5.63},
        {"name": "招商银行", "change": 1.05},
        {"name": "阿里巴巴", "change": 2.78},
        {"name": "药明康德", "change": -0.92},
    ]
    html = render_template(
        "pages/market_overview.html",
        active_page="home",
        index_cards=index_cards,
        hot_stocks=hot_stocks,
    )
    return HTMLResponse(html)


@app.get("/hot-news", response_class=HTMLResponse)
async def hot_news(request: Request):
    """Hot news page."""
    from notifier import TIER_LABELS, TIER_COLORS, TIER_BG

    stats = [
        {"label": "今日热点", "value": "86", "icon": "flame",
         "bg": "hsl(var(--primary) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "新闻来源", "value": "7", "icon": "newspaper",
         "bg": "hsl(var(--info) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "热点新闻", "value": "8", "icon": "star",
         "bg": "hsl(var(--warning) / 0.1)", "color": "hsl(var(--foreground))"},
        {"label": "利好指数", "value": "62%", "icon": "trending-up-lg",
         "bg": "hsl(var(--danger) / 0.1)", "color": "hsl(var(--danger))"},
    ]
    tier_labels = [
        {"label": f"T{t}·{TIER_LABELS[t].split('·')[1]}", "color": c, "bg": TIER_BG[t]}
        for t, c in TIER_COLORS.items()
    ]
    keywords = ["央行", "AI", "港股", "外资", "芯片", "新能源"]

    tier1_cards = [
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "华尔街见闻", "time": "2h前", "heat": "98",
            "title": "央行降准释放万亿流动性，A股三大指数集体高开",
            "summary": "中国人民银行宣布下调金融机构存款准备金率0.5个百分点，预计释放长期资金约1.2万亿元。分析人士认为此举将有效降低企业融资成本，支持实体经济持续恢复。",
            "points": ["银行板块全线飘红，招商银行涨超3%", "券商板块联动上涨，中信证券涨逾2%"],
            "keywords": [
                {"text": "央行", "primary": True}, {"text": "降准", "primary": False},
                {"text": "流动性", "primary": False}, {"text": "A股", "primary": False},
            ],
        },
        {
            "sentiment": "利空", "sentiment_bg": "hsl(var(--success) / 0.1)",
            "sentiment_color": "hsl(var(--success))",
            "source": "证券时报", "time": "1h前", "heat": "95",
            "title": "美联储暗示6月暂停加息，全球风险资产应声大涨",
            "summary": "美联储主席鲍威尔在国会听证中释放明确鸽派信号，市场对6月加息概率预期从70%骤降至15%。纳指、标普500盘中双双创下历史新高。",
            "keywords": [
                {"text": "美联储", "primary": True}, {"text": "利率", "primary": False},
                {"text": "美股", "primary": False}, {"text": "鲍威尔", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "36氪", "time": "3h前", "heat": "91",
            "title": "OpenAI 发布新一代推理模型，AI 芯片需求预期上调",
            "summary": "新模型在多项基准测试中大幅领先，推理效率提升3倍。多家中外券商同步上调英伟达、台积电目标价，AI算力产业链全面走强。",
            "keywords": [
                {"text": "AI", "primary": True}, {"text": "芯片", "primary": False},
                {"text": "英伟达", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "财联社", "time": "30min前", "heat": "93",
            "title": "北向资金今日净买入超120亿，连续5日净流入创年内纪录",
            "summary": "沪股通净买入58亿，深股通净买入62亿，外资加速回流中国资产。大金融、白酒板块获集中加仓，贵州茅台、招商银行位列净买入榜首。",
            "points": ["连续5日净流入累计超400亿元", "单日净买入额创年内新高"],
            "keywords": [
                {"text": "北向资金", "primary": True}, {"text": "外资", "primary": False},
                {"text": "茅台", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "澎湃新闻", "time": "4h前", "heat": "88",
            "title": "新能源汽车渗透率突破45%，行业拐点已至",
            "summary": "工信部最新数据显示，5月新能源乘用车零售渗透率达45.2%，充电桩、动力电池板块集体爆发，宁德时代盘中涨逾4%。",
            "keywords": [
                {"text": "新能源汽车", "primary": True}, {"text": "宁德时代", "primary": False},
                {"text": "充电桩", "primary": False},
            ],
        },
        {
            "sentiment": "中性", "sentiment_bg": "hsl(var(--warning) / 0.1)",
            "sentiment_color": "hsl(var(--warning))",
            "source": "财联社", "time": "5h前", "heat": "76",
            "title": "比亚迪新车发布，新能源SUV市场格局生变",
            "summary": "比亚迪全新SUV起售价14.98万元，远低于市场预期的16-18万元区间。竞品股价普遍承压，理想、蔚来港股跌幅超过4%。",
            "keywords": [
                {"text": "比亚迪", "primary": True}, {"text": "SUV", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "今日头条", "time": "2h前", "heat": "85",
            "title": "恒生科技指数涨超3%，腾讯阿里双双走强",
            "summary": "港股科技股集体爆发，恒生科技指数创近三个月新高。腾讯控股涨超4%，阿里巴巴涨逾3%，南向资金连续第8个交易日净流入。",
            "keywords": [
                {"text": "港股", "primary": True}, {"text": "腾讯", "primary": False},
                {"text": "阿里", "primary": False},
            ],
        },
        {
            "sentiment": "利好", "sentiment_bg": "hsl(var(--danger) / 0.1)",
            "sentiment_color": "hsl(var(--danger))",
            "source": "澎湃新闻", "time": "6h前", "heat": "72",
            "title": "国务院印发数字经济发展规划，数据要素市场迎重磅利好",
            "summary": "规划明确提出到2027年数字经济核心产业增加值占GDP比重达到12%，数据要素、信创、国产软件等板块迎来政策催化。",
            "keywords": [
                {"text": "数字经济", "primary": True}, {"text": "数据要素", "primary": False},
                {"text": "信创", "primary": False},
            ],
        },
    ]

    list_items = [
        {"seq": 1, "title": "北向资金今日净买入超120亿，连续5日净流入", "source": "华尔街见闻"},
        {"seq": 2, "title": "恒生科技指数涨超3%，腾讯阿里双双走强", "source": "财联社"},
        {"seq": 3, "title": "比特币重返6.5万美元，加密概念股集体上涨", "source": "今日头条"},
        {"seq": 4, "title": "国务院印发数字经济发展规划，数据要素市场迎利好", "source": "澎湃新闻"},
        {"seq": 5, "title": "贵州茅台宣布特别分红，股息率创历史新高", "source": "证券时报"},
        {"seq": 6, "title": "多地放松楼市限购，房地产板块触底反弹", "source": "36氪"},
        {"seq": 7, "title": "中芯国际14nm良率突破，国产替代加速推进", "source": "财联社"},
        {"seq": 8, "title": "光伏组件价格触底，龙头厂商宣布联合限产", "source": "澎湃新闻"},
        {"seq": 9, "title": "瑞银上调中国GDP增速预期至5.3%", "source": "证券时报"},
        {"seq": 10, "title": "科创50ETF份额突破千亿，资金持续涌入", "source": "今日头条"},
    ]

    html = render_template(
        "pages/hot_news.html",
        active_page="hot-news",
        stats=stats,
        tier_labels=tier_labels,
        keywords=keywords,
        tier1_cards=tier1_cards,
        list_items=list_items,
        total_count=78,
        page_start=1,
        page_end=10,
        current_page=1,
        page_numbers=[1, 2, 3, "...", 8],
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web:app", host="0.0.0.0", port=8000, reload=True)
