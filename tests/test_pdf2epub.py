"""Tests for the PDF → ePUB pipeline.

The PDF fixtures in tests/fixtures/ were rendered by headless Chromium from
the HTML in make_fixtures.py, so they exercise the same glyph layout that
Vivliostyle (this app's own PDF output) produces: stacked one-glyph lines for
vertical text, ruby as separate small runs, and Type3 vertical glyphs.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pymupdf
import pytest

import pdf2epub
from pdf2epub import (
    Glyph, Run, ScriptStats, Unit, _band_signature, _inline_html, _merge_runs_to_glyphs,
    reading_order, resolve_language,
)

FIXTURES = Path(__file__).parent / "fixtures"
XHTML_NS = "{http://www.w3.org/1999/xhtml}"


# --- helpers ----------------------------------------------------------------
def _convert(name: str, tmp_path: Path, **kw) -> tuple[pdf2epub.ConvertResult, zipfile.ZipFile]:
    out = tmp_path / f"{name}.epub"
    res = pdf2epub.convert(FIXTURES / f"{name}.pdf", out, **kw)
    return res, zipfile.ZipFile(out)


def _chapters(zf: zipfile.ZipFile) -> list[str]:
    names = sorted(n for n in zf.namelist() if n.startswith("OEBPS/text/"))
    return [zf.read(n).decode("utf-8") for n in names]


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


def assert_valid_epub_structure(zf: zipfile.ZipFile) -> None:
    names = zf.namelist()
    # mimetype first, stored, exact content
    assert names[0] == "mimetype"
    info = zf.getinfo("mimetype")
    assert info.compress_type == zipfile.ZIP_STORED
    assert zf.read("mimetype") == b"application/epub+zip"
    assert "META-INF/container.xml" in names
    opf = ET.fromstring(zf.read("OEBPS/content.opf"))
    ns = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    assert opf.find("opf:metadata/dc:title", ns) is not None
    assert opf.find("opf:metadata/dc:language", ns) is not None
    hrefs = [i.get("href") for i in opf.findall("opf:manifest/opf:item", ns)]
    for href in hrefs:
        assert f"OEBPS/{href}" in names, f"manifest item {href} missing from zip"
    ids = {i.get("id") for i in opf.findall("opf:manifest/opf:item", ns)}
    for ref in opf.findall("opf:spine/opf:itemref", ns):
        assert ref.get("idref") in ids
    # every XHTML document must be well-formed XML
    for n in names:
        if n.endswith(".xhtml"):
            ET.fromstring(zf.read(n))


# --- unit-level ---------------------------------------------------------------
class TestLanguageGuess:
    def test_japanese(self):
        s = ScriptStats(); s.add_text("吾輩は猫である。名前はまだ無い。")
        assert s.guess_language() == "ja"

    def test_korean(self):
        s = ScriptStats(); s.add_text("한국의 봄은 삼월에 시작된다.")
        assert s.guess_language() == "ko"

    def test_traditional_chinese(self):
        s = ScriptStats(); s.add_text("這個國家的學生們說，時間會證明一切。")
        assert s.guess_language() == "zh-TW"

    def test_simplified_chinese(self):
        s = ScriptStats(); s.add_text("这个国家的学生们说，时间会证明一切。")
        assert s.guess_language() == "zh-CN"

    def test_english(self):
        s = ScriptStats(); s.add_text("The quick brown fox jumps over the lazy dog.")
        assert s.guess_language() == "en"

    def test_pua_fraction(self):
        s = ScriptStats(); s.add_text("中")
        assert s.pua_fraction == pytest.approx(0.75)


class TestResolveLanguage:
    def test_catalog_wins_when_plausible(self):
        assert resolve_language("ja-JP", "ja") == "ja-JP"

    def test_bare_zh_refined_by_script(self):
        assert resolve_language("zh", "zh-TW") == "zh-TW"

    def test_wrong_english_declaration_overridden(self):
        assert resolve_language("en-US", "ja") == "ja"

    def test_garbage_ignored(self):
        assert resolve_language("not a tag!", "ko") == "ko"

    def test_none(self):
        assert resolve_language(None, "en") == "en"


def test_band_signature_collapses_page_numbers():
    assert _band_signature("Chapter One · 17") == _band_signature("Chapter One · 132")
    assert _band_signature("第三章　12") == _band_signature("第三章　345")


def _run(text: str, x0: float, y0: float, size: float = 10.0, *, bold=False, italic=False,
         vertical=False) -> Run:
    glyphs = []
    for i, ch in enumerate(text):
        if vertical:
            g = Glyph(ch, x0, y0 + i * size, x0 + size, y0 + (i + 1) * size, size, bold, italic)
        else:
            g = Glyph(ch, x0 + i * size * 0.5, y0, x0 + (i + 1) * size * 0.5, y0 + size, size, bold, italic)
        glyphs.append(g)
    return Run(glyphs, vertical, min(g.x0 for g in glyphs), min(g.y0 for g in glyphs),
               max(g.x1 for g in glyphs), max(g.y1 for g in glyphs))


class TestInlineHtml:
    def test_dehyphenation_and_spacing(self):
        a = _run("The commu-", 0, 0)
        b = _run("nication failed", 0, 12)
        glyphs = "".join(g.c for g, _ in _merge_runs_to_glyphs([a, b]))
        assert glyphs == "The communication failed"

    def test_cjk_lines_join_without_space(self):
        a = _run("吾輩は猫で", 0, 0)
        b = _run("ある。", 0, 12)
        assert _text(_inline_html([a, b])) == "吾輩は猫である。"

    def test_hangul_lines_join_without_space(self):
        # Renderers break Korean between syllables, so no space is inserted.
        a = _run("내리기도", 0, 0)
        b = _run("한다.", 0, 12)
        assert _text(_inline_html([a, b])) == "내리기도한다."

    def test_bold_italic_and_links(self):
        r = _run("ab cd", 0, 0)
        r.glyphs[0].bold = r.glyphs[1].bold = True
        r.glyphs[3].italic = r.glyphs[4].italic = True
        r.glyphs[3].link = r.glyphs[4].link = "https://x.y/?a=1&b=2"
        html = _inline_html([r])
        assert html == '<strong>ab</strong> <a href="https://x.y/?a=1&amp;b=2"><em>cd</em></a>'

    def test_ruby_markup(self):
        r = _run("吾輩は", 0, 0)
        r.rubies.append("わがはい")
        r.glyphs[0].ruby = r.glyphs[1].ruby = 0
        assert _inline_html([r]) == "<ruby>吾輩<rt>わがはい</rt></ruby>は"

    def test_escaping(self):
        r = _run("a<b&c", 0, 0)
        assert _inline_html([r]) == "a&lt;b&amp;c"

    def test_uniform_bold_heading_drops_strong(self):
        r = _run("Title", 0, 0, bold=True)
        assert _inline_html([r], in_heading=True) == "Title"


def _unit(x0, y0, x1, y1, tag="") -> Unit:
    u = Unit("text", x0, y0, x1, y1, 0)
    u.html = tag
    return u


class TestReadingOrder:
    def test_two_columns_under_a_heading(self):
        heading = _unit(50, 50, 350, 70, "H")
        left = [_unit(50, 100 + i * 14, 190, 112 + i * 14, f"L{i}") for i in range(5)]
        right = [_unit(210, 100 + i * 14, 350, 112 + i * 14, f"R{i}") for i in range(5)]
        units = [heading] + [u for pair in zip(left, right) for u in pair]
        order = [u.html for u in reading_order(units, vertical=False, body_size=10)]
        assert order == ["H"] + [f"L{i}" for i in range(5)] + [f"R{i}" for i in range(5)]

    def test_vertical_columns_read_right_to_left(self):
        cols = [_unit(300 - i * 20, 50, 312 - i * 20, 400, f"C{i}") for i in range(4)]
        order = [u.html for u in reading_order(list(reversed(cols)), vertical=True, body_size=12)]
        assert order == ["C0", "C1", "C2", "C3"]

    def test_vertical_bands_top_then_bottom(self):
        top = [_unit(300 - i * 20, 50, 312 - i * 20, 200, f"T{i}") for i in range(3)]
        bottom = [_unit(300 - i * 20, 240, 312 - i * 20, 400, f"B{i}") for i in range(3)]
        order = [u.html for u in reading_order(bottom + top, vertical=True, body_size=12)]
        assert order == ["T0", "T1", "T2", "B0", "B1", "B2"]


# --- validation -------------------------------------------------------------
class TestValidate:
    def test_not_a_pdf(self, tmp_path):
        p = tmp_path / "x.pdf"; p.write_bytes(b"hello")
        with pytest.raises(pdf2epub.PdfError, match="not a valid PDF"):
            pdf2epub.validate(p)

    def test_password_protected(self, tmp_path):
        doc = pymupdf.open(); page = doc.new_page()
        page.insert_text((50, 50), "secret", fontname="helv")
        p = tmp_path / "enc.pdf"
        doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
        with pytest.raises(pdf2epub.PdfError, match="password"):
            pdf2epub.validate(p)

    def test_good_pdf(self):
        pdf2epub.validate(FIXTURES / "en_book.pdf")


# --- end-to-end on fixtures -------------------------------------------------
@pytest.fixture(scope="module")
def en_book(tmp_path_factory):
    return _convert("en_book", tmp_path_factory.mktemp("en"))


@pytest.fixture(scope="module")
def ja_book(tmp_path_factory):
    return _convert("ja_vertical", tmp_path_factory.mktemp("ja"))


class TestEnglishBook:
    @pytest.fixture
    def book(self, en_book):
        return en_book

    def test_structure(self, book):
        res, zf = book
        assert_valid_epub_structure(zf)
        assert res.language == "en" and not res.vertical
        assert res.title == "The Fixture Book"

    def test_headings_and_bookmarks_become_nav(self, book):
        _res, zf = book
        nav = zf.read("OEBPS/nav.xhtml").decode()
        assert "Chapter One: Beginnings" in nav and "Chapter Two: Two Columns" in nav
        # nested: the h2 sits inside the first chapter's <li>
        assert re.search(r"Chapter One: Beginnings</a><ol><li><a[^>]*>A Sub-Section", nav)
        ncx = zf.read("OEBPS/toc.ncx").decode()
        assert ncx.count("<navPoint") == 3
        chapters = _chapters(zf)
        assert any("<h1" in c and "Chapter One: Beginnings" in c for c in chapters)
        assert any("<h2" in c and "A Sub-Section" in c for c in chapters)

    def test_chapter_files_split_at_level_one_bookmarks(self, book):
        res, zf = book
        assert res.chapter_count == 3  # title page, chapter one, chapter two

    def test_running_heads_and_page_numbers_removed(self, book):
        _res, zf = book
        body = " ".join(_text(c) for c in _chapters(zf))
        assert "Running Head" not in body
        assert "The Fixture Book" in body  # the title page keeps its title

    def test_paragraph_healed_across_page_break(self, book):
        _res, zf = book
        chapters = _chapters(zf)
        para = next(p for c in chapters for p in re.findall(r"<p[^>]*>.*?</p>", c, re.S)
                    if "deliberately long" in p)
        assert "PARAGRAPH-END" in para

    def test_inline_styles_links_and_superscript(self, book):
        _res, zf = book
        html = "".join(_chapters(zf))
        assert "<strong>first</strong>" in html
        assert "<em>emphasis</em>" in html
        assert "<sup>1</sup>" in html
        assert '<a href="https://example.org/reference">an external reference</a>' in html
        # internal link resolves to the chapter file that holds page 4
        m = re.search(r'<a href="(ch\d+\.xhtml)#(pg\d+)">Chapter Two</a>', html)
        assert m, html
        target = zf.read(f"OEBPS/text/{m.group(1)}").decode()
        assert f'id="{m.group(2)}"' in target and "Chapter Two: Two Columns" in target

    def test_image_and_table(self, book):
        res, zf = book
        html = "".join(_chapters(zf))
        assert res.image_count >= 1
        imgs = re.findall(r'<img src="\.\./images/([^"]+)"', html)
        assert imgs
        for name in imgs:
            assert f"OEBPS/images/{name}" in zf.namelist()
        assert "<table>" in html and "<th>Quantity</th>" in html and "<td>2.50</td>" in html
        assert "Figure 1: a two-colour rectangle" in html

    def test_two_columns_read_in_order(self, book):
        _res, zf = book
        text = _text("".join(_chapters(zf)))
        assert text.index("LEFT-COLUMN-START") < text.index("LEFT-COLUMN-END") \
            < text.index("RIGHT-COLUMN-START") < text.index("RIGHT-COLUMN-END")

    def test_cover(self, book):
        _res, zf = book
        opf = zf.read("OEBPS/content.opf").decode()
        assert 'properties="cover-image"' in opf
        assert "OEBPS/cover.xhtml" in zf.namelist()


class TestJapaneseVertical:
    @pytest.fixture
    def book(self, ja_book):
        return ja_book

    def test_vertical_writing_mode_and_rtl_spine(self, book):
        res, zf = book
        assert_valid_epub_structure(zf)
        assert res.vertical and res.language == "ja"
        assert res.title == "吾輩は猫である"
        opf = zf.read("OEBPS/content.opf").decode()
        assert 'page-progression-direction="rtl"' in opf
        assert 'primary-writing-mode" content="vertical-rl"' in opf
        css = zf.read("OEBPS/styles.css").decode()
        assert "writing-mode: vertical-rl" in css and "-epub-writing-mode: vertical-rl" in css
        for c in _chapters(zf):
            assert 'xml:lang="ja"' in c

    def test_columns_rebuilt_into_paragraphs(self, book):
        _res, zf = book
        html = "".join(_chapters(zf))
        assert "は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。" in _text(html)
        # each source paragraph is one <p>, not one per column
        assert "<p>掌の上で少し落ちついて書生の顔を見たのがいわゆる人間というものの見始であろう。この時妙なものだと思った感じが今でも残っている。</p>" in html

    def test_ruby(self, book):
        _res, zf = book
        html = "".join(_chapters(zf))
        assert "<ruby>吾輩<rt>わがはい</rt></ruby>は猫である" in html
        assert "<ruby>書生<rt>しょせい</rt></ruby>" in html
        assert "<ruby>獰悪<rt>どうあく</rt></ruby>" in html

    def test_headings_keep_ideographic_space(self, book):
        _res, zf = book
        html = "".join(_chapters(zf))
        assert "第一章　夜明け</h1>" in html
        assert "第一節　書生</h2>" in html

    def test_links(self, book):
        _res, zf = book
        html = "".join(_chapters(zf))
        assert '<a href="https://example.com/neko">参考リンク</a>' in html
        assert re.search(r'<a href="ch0001\.xhtml#(pg0|toc0)">第一章へ戻る</a>', html)

    def test_nav_from_bookmarks(self, book):
        _res, zf = book
        nav = zf.read("OEBPS/nav.xhtml").decode()
        assert "第一章　夜明け" in nav and "第二章　再会" in nav
        assert re.search(r"第一章　夜明け</a><ol><li><a[^>]*>第一節　書生", nav)

    def test_image_and_centered_line(self, book):
        res, zf = book
        html = "".join(_chapters(zf))
        assert res.image_count == 1 and '<img src="../images/' in html
        assert '<p class="center">（了）</p>' in html


class TestTraditionalChinese:
    def test_language_ruby_and_indent(self, tmp_path):
        res, zf = _convert("zh_tw_horizontal", tmp_path)
        assert_valid_epub_structure(zf)
        assert res.language == "zh-TW" and not res.vertical
        html = "".join(_chapters(zf))
        assert "<ruby>玉<rt>ㄩˋ</rt></ruby><ruby>山<rt>ㄕㄢ</rt></ruby>" in html
        assert "第一章　山的呼喚</h1>" in html
        css = zf.read("OEBPS/styles.css").decode()
        assert "text-indent: 2em" in css


class TestKorean:
    def test_language_and_headings(self, tmp_path):
        res, zf = _convert("ko_horizontal", tmp_path)
        assert_valid_epub_structure(zf)
        assert res.language == "ko"
        html = "".join(_chapters(zf))
        assert "제1장 봄</h1>" in html and "제2장 여름</h1>" in html
        assert "한국의 봄은 삼월에 시작된다." in _text(html)


class TestScannedPdf:
    def test_pages_become_images_when_ocr_off(self, tmp_path):
        # a PDF whose pages are only raster images (a scan)
        src = pymupdf.open(str(FIXTURES / "zh_tw_horizontal.pdf"))
        pix = src[0].get_pixmap(dpi=60)
        doc = pymupdf.open()
        for _ in range(2):
            page = doc.new_page(width=420, height=595)
            page.insert_image(page.rect, pixmap=pix)
        p = tmp_path / "scan.pdf"; doc.save(str(p))
        res = pdf2epub.convert(p, tmp_path / "scan.epub", ocr_mode="off")
        assert res.image_count >= 1
        assert any("images" in n for n in res.notes)
        zf = zipfile.ZipFile(tmp_path / "scan.epub")
        assert_valid_epub_structure(zf)
        assert "<img" in "".join(_chapters(zf))


class TestNoBookmarks:
    def test_nav_built_from_detected_headings(self, tmp_path):
        doc = pymupdf.open()
        for i in range(2):
            page = doc.new_page(width=420, height=595)
            page.insert_text((40, 60), f"Part {i + 1}", fontsize=22, fontname="helv")
            y = 100
            for _ in range(12):
                page.insert_text((40, y), "Body text of the section, repeated line after line.", fontsize=10, fontname="helv")
                y += 14
        p = tmp_path / "plain.pdf"; doc.save(str(p))
        res = pdf2epub.convert(p, tmp_path / "plain.epub")
        zf = zipfile.ZipFile(tmp_path / "plain.epub")
        assert_valid_epub_structure(zf)
        nav = zf.read("OEBPS/nav.xhtml").decode()
        assert "Part 1" in nav and "Part 2" in nav
        assert res.chapter_count == 2
        html = "".join(_chapters(zf))
        assert "<h1" in html and "Part 1</h1>" in html
