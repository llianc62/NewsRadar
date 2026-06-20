"""Download Google Fonts woff2 files for self-hosting.

Downloads only the *latin* subset (covers Chinese-friendly Latin characters)
and deduplicates by URL — Google Fonts often serves the same variable-font
file for multiple static weight instances, so 4 declared weights may map to
a single downloaded file.

Usage: python scripts/download_fonts.py
"""
from __future__ import annotations

import re
import urllib.request
from pathlib import Path

FONTS_DIR = Path(__file__).parent.parent / "web" / "static" / "fonts"
FONTS_CSS_PATH = Path(__file__).parent.parent / "web" / "static" / "css" / "fonts.css"

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

FAMILY_STEMS = {
    "Newsreader": "newsreader",
    "DM Sans": "dmsans",
    "JetBrains Mono": "jetbrainsmono",
}

LATIN_MARKER = "U+0000-00FF"

# Regex to split CSS into @font-face blocks
_BLOCK_RE = re.compile(r"@font-face\s*\{([^}]+)\}", re.DOTALL)


def main() -> None:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch the Google Fonts CSS ----------------------------------
    print("Fetching Google Fonts CSS...")
    req = urllib.request.Request(GOOGLE_FONTS_CSS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        css_text = resp.read().decode("utf-8")

    # 2. Parse each @font-face block ---------------------------------
    #    Build: family -> weight -> list of (is_latin, url)
    candidates: dict[str, dict[str, list[tuple[bool, str]]]] = {}

    for block_text in _BLOCK_RE.findall(css_text):
        family_m = re.search(r"font-family:\s*'([^']+)'", block_text)
        weight_m = re.search(r"font-weight:\s*(\d+)", block_text)
        url_m = re.search(r"url\(([^)]+)\)", block_text)
        unicode_m = re.search(r"unicode-range:\s*([^;]+);", block_text)

        if not (family_m and weight_m and url_m):
            continue
        family = family_m.group(1)
        if family not in FAMILY_STEMS:
            continue

        weight = weight_m.group(1)
        is_latin = LATIN_MARKER in (unicode_m.group(1) if unicode_m else "")
        url = url_m.group(1)

        candidates.setdefault(family, {}).setdefault(weight, []).append(
            (is_latin, url)
        )

    print(f"  Parsed {sum(len(w) for w in candidates.values())} weights across {len(candidates)} families")

    # 3. Select best URL per (family, weight) — prefer latin ---------
    #    url_to_stem: unique URL -> basename stem (no .woff2 extension)
    url_to_stem: dict[str, str] = {}
    #    weight_map: family -> weight -> stem (for CSS generation)
    weight_map: dict[str, dict[str, str]] = {}

    for family, weights in candidates.items():
        weight_map[family] = {}
        for weight, options in weights.items():
            # Prefer latin, fall back to first option
            best = next(
                (opt for opt in options if opt[0]),
                options[0],
            )
            url = best[1]

            if url not in url_to_stem:
                base = FAMILY_STEMS[family]
                # If the same base is already used by a different URL,
                # append the weight (JetBrains Mono case)
                existing = [s for s in url_to_stem.values() if s.startswith(base)]
                if existing:
                    stem = f"{base}-{weight}"
                else:
                    stem = base
                url_to_stem[url] = stem
            weight_map[family][weight] = url_to_stem[url]

    print(f"  {len(url_to_stem)} unique file(s) to download")

    # 4. Download unique font files ----------------------------------
    for url, stem in url_to_stem.items():
        filepath = FONTS_DIR / f"{stem}.woff2"
        print(f"  → {stem}.woff2")
        urllib.request.urlretrieve(url, filepath)
        print(f"    {filepath.stat().st_size:,} bytes")

    # Clean up old font files that are no longer needed
    expected_names = {f"{s}.woff2" for s in url_to_stem.values()}
    for old_file in FONTS_DIR.glob("*.woff2"):
        if old_file.name not in expected_names:
            print(f"  ✕ removing stale file: {old_file.name}")
            old_file.unlink()

    print(f"\nDone — {len(url_to_stem)} font files in {FONTS_DIR}")

    # 5. Generate fonts.css ------------------------------------------
    css_lines = [
        "/* ── Self-hosted fonts (auto-generated) ── */",
        "/*    Run:  python scripts/download_fonts.py   */",
        "/*    All fonts use font-display: swap — text   */",
        "/*    renders immediately with fallback font.   */",
        "",
    ]

    for family in sorted(weight_map):
        weights = weight_map[family]
        css_lines.append(f"/* {family} */")
        for weight in sorted(weights, key=int):
            stem = weights[weight]
            css_lines.extend([
                "@font-face {",
                f"  font-family: '{family}';",
                "  font-style: normal;",
                f"  font-weight: {weight};",
                "  font-display: swap;",
                f"  src: url('/static/fonts/{stem}.woff2') format('woff2');",
                "}",
            ])
        css_lines.append("")

    fonts_css = "\n".join(css_lines)
    FONTS_CSS_PATH.write_text(fonts_css, encoding="utf-8")
    print(f"Generated {FONTS_CSS_PATH}")


if __name__ == "__main__":
    main()
