"""Shared test utilities for parser tests."""


def make_html(body: str, head_noise: str = "", tail_noise: str = "") -> str:
    """Build a minimal HTML page with optional head/tail noise around body.

    Moved from tests/test_parser.py:_make_html so all parser test files
    can share the same HTML construction helper.
    """
    return f"""<!DOCTYPE html>
<html>
<head><title>Test Article</title></head>
<body>
<article>
{head_noise}
{body}
{tail_noise}
</article>
</body>
</html>"""
