"""Download Google Fonts woff2 files for self-hosting.

Usage: python scripts/download_fonts.py
"""
import re
import urllib.request
from pathlib import Path

FONTS_DIR = Path(__file__).parent.parent / "web" / "static" / "fonts"

# Modern Chrome user-agent to get woff2 URLs
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

GOOGLE_FONTS_CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600;6..72,700"
    "&family=DM+Sans:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@400;500;600"
    "&display=swap"
)

FONT_WEIGHT_MAP = {
    "Newsreader": {
        "400": "newsreader-400.woff2",
        "500": "newsreader-500.woff2",
        "600": "newsreader-600.woff2",
        "700": "newsreader-700.woff2",
    },
    "DM Sans": {
        "400": "dmsans-400.woff2",
        "500": "dmsans-500.woff2",
        "600": "dmsans-600.woff2",
        "700": "dmsans-700.woff2",
    },
    "JetBrains Mono": {
        "400": "jetbrainsmono-400.woff2",
        "500": "jetbrainsmono-500.woff2",
        "600": "jetbrainsmono-600.woff2",
    },
}


def main():
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch the Google Fonts CSS
    print("Fetching Google Fonts CSS...")
    req = urllib.request.Request(GOOGLE_FONTS_CSS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        css_text = resp.read().decode("utf-8")

    # 2. Extract woff2 URLs with font-family and font-weight context
    # The CSS structure is:
    #   @font-face { font-family: 'Newsreader'; font-weight: 400; src: url(...) format('woff2'); }
    #   /* latin-ext */ @font-face { ... }  (skip non-latin subsets if already have latin)
    pattern = re.compile(
        r"@font-face\s*\{[^}]*"
        r"font-family:\s*'([^']+)'[^}]*"
        r"font-weight:\s*(\d+)[^}]*"
        r"src:\s*url\(([^)]+)\)[^}]*"
        r"format\('woff2'\)",
        re.DOTALL,
    )

    downloaded = set()
    for match in pattern.finditer(css_text):
        family = match.group(1)
        weight = match.group(2)
        url = match.group(3)
        filename = FONT_WEIGHT_MAP.get(family, {}).get(weight)
        if not filename or filename in downloaded:
            continue
        downloaded.add(filename)
        filepath = FONTS_DIR / filename
        print(f"  {family} wght@{weight} -> {filename}")
        urllib.request.urlretrieve(url, filepath)
        print(f"    saved ({filepath.stat().st_size} bytes)")

    print(f"\nDownloaded {len(downloaded)} font files to {FONTS_DIR}")


if __name__ == "__main__":
    main()
