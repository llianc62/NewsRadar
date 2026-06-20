"""Tests for HtmlParser._fix_lazy_images — lazy-loaded image src rewriting."""

from news.parser import HtmlParser


class TestFixLazyImages:
    """_fix_lazy_images converts data-src / data-original to src
    when the placeholder is a data: URI."""

    def test_swaps_data_src_with_data_uri_placeholder(self):
        html = '<img data-src="https://x.com/real.jpg" src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==">'
        result = HtmlParser._fix_lazy_images(html)
        assert 'src="https://x.com/real.jpg"' in result
        assert "data-src" not in result

    def test_swaps_data_original_with_data_uri_placeholder(self):
        html = '<img class="lazy" data-original="https://x.com/real.png" src="data:image/png;base64,iVBORw0KGgo=">'
        result = HtmlParser._fix_lazy_images(html)
        assert 'src="https://x.com/real.png"' in result
        assert "data-original" not in result

    def test_preserves_normal_img_unchanged(self):
        # Normal img without lazy-load placeholder — must stay intact
        html = '<img src="https://x.com/normal.jpg" alt="正常图片">'
        result = HtmlParser._fix_lazy_images(html)
        assert result == html
