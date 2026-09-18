"""Regenerate the PDF fixtures used by tests/test_pdf2epub.py.

The fixtures are produced by headless Chromium (the same engine Vivliostyle
uses, so they are representative of this app's own PDF output: Skia emits
vertical CJK as stacked one-glyph lines and Type3 fonts). Run this script
when the HTML sources below change:

    python tests/fixtures/make_fixtures.py [--chromium /path/to/chromium]

Requires a Chromium binary and CJK fonts (Noto CJK, IPAGothic or WenQuanYi).
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

_EN_CSS = """
@page {
  size: 148mm 210mm; margin: 18mm 14mm;
  /* real running head + page numbers via CSS page margin boxes */
  @top-left { content: "The Fixture Book"; font-size: 8pt; color: #555; }
  @top-right { content: "Running Head"; font-size: 8pt; color: #555; }
  @bottom-center { content: counter(page); font-size: 8pt; color: #555; }
}
html { font-family: "Liberation Serif", "Noto Serif", "DejaVu Serif", serif; font-size: 11pt; line-height: 1.55; }
body { margin: 0; }
h1 { font-size: 22pt; margin: 0 0 12pt 0; break-before: page; }
h1.first { break-before: auto; }
h2 { font-size: 15pt; margin: 14pt 0 6pt 0; }
p { margin: 0; text-indent: 1.5em; text-align: justify; }
p.first, h1 + p, h2 + p { text-indent: 0; }
p.center { text-align: center; text-indent: 0; }
.titlepage { break-after: page; padding-top: 55mm; text-align: center; }
.title { font-size: 26pt; font-weight: bold; }
.author { font-size: 13pt; margin-top: 8pt; }
.cols { column-count: 2; column-gap: 8mm; }
table { border-collapse: collapse; margin: 8pt 0; font-size: 9.5pt; }
td, th { border: 1px solid #333; padding: 2pt 6pt; }
figure { margin: 8pt 0; text-align: center; }
a { color: #0645ad; }
sup { font-size: 0.7em; }
"""


def _png_data_uri(width: int = 120, height: int = 80) -> str:
    """A small red/blue PNG without needing Pillow at test time."""
    try:
        from PIL import Image
        im = Image.new("RGB", (width, height), (200, 40, 40))
        for x in range(width // 2, width):
            for y in range(height):
                im.putpixel((x, y), (40, 40, 200))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        data = buf.getvalue()
    except Exception:  # pragma: no cover
        # 1x1 red PNG
        data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    return "data:image/png;base64," + base64.b64encode(data).decode()


def en_book() -> str:
    img = _png_data_uri()
    lorem = ("The quick brown fox jumps over the lazy dog while the five boxing wizards jump quickly. "
             "Pack my box with five dozen liquor jugs. How vexingly quick daft zebras jump. ")
    long_para = ("This paragraph is deliberately long so that it runs across a page break, which the converter "
                 "must heal by joining the two halves into one paragraph. " + lorem * 9 + "PARAGRAPH-END.")
    body = f"""
<div class="titlepage"><div class="title">The Fixture Book</div><div class="author">by A. N. Author</div></div>
<h1 id="ch1" class="first">Chapter One: Beginnings</h1>
<p>This is the <strong>first</strong> paragraph of the book, and it has <em>emphasis</em> and a footnote marker<sup>1</sup>. {lorem}</p>
<p>The second paragraph mentions <a href="https://example.org/reference">an external reference</a> and jumps to <a href="#ch2">Chapter Two</a>. {lorem}</p>
<h2 id="s11">A Sub-Section</h2>
<p>{lorem}{lorem}</p>
<p>{long_para}</p>
<p>A short paragraph after the long one.</p>
<figure><img src="{img}" width="120" height="80" alt="figure"/></figure>
<p class="center">Figure 1: a two-colour rectangle</p>
<table>
  <tr><th>Item</th><th>Quantity</th><th>Price</th></tr>
  <tr><td>Apples</td><td>3</td><td>1.20</td></tr>
  <tr><td>Pears</td><td>5</td><td>2.50</td></tr>
</table>
<p>{lorem}</p>
<h1 id="ch2">Chapter Two: Two Columns</h1>
<div class="cols">
  <p class="first">LEFT-COLUMN-START {lorem}{lorem}{lorem} LEFT-COLUMN-END</p>
  <p class="first">RIGHT-COLUMN-START {lorem}{lorem} RIGHT-COLUMN-END</p>
</div>
"""
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>The Fixture Book</title>
<style>{_EN_CSS}</style></head><body>{body}</body></html>"""


def ja_vertical() -> str:
    img = _png_data_uri(90, 120)
    css = """
@page { size: 148mm 210mm; margin: 15mm; }
html { writing-mode: vertical-rl; font-family: "Noto Serif CJK JP", "IPAGothic", "WenQuanYi Zen Hei", serif; font-size: 12pt; line-height: 1.8; }
h1 { font-size: 20pt; margin: 0 0 0 12pt; }
h2 { font-size: 16pt; margin: 0 0 0 8pt; }
p { text-indent: 1em; margin: 0; }
rt { font-size: 6pt; }
.center { text-align: center; }
figure { margin: 0 1em; }
a { color: #0055aa; }
"""
    return f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><title>吾輩は猫である</title><style>{css}</style></head><body>
<h1 id="c1">第一章　夜明け</h1>
<p><ruby>吾輩<rt>わがはい</rt></ruby>は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。</p>
<p>吾輩はここで始めて人間というものを見た。しかもあとで聞くとそれは<ruby>書生<rt>しょせい</rt></ruby>という人間中で一番<ruby>獰悪<rt>どうあく</rt></ruby>な種族であったそうだ。</p>
<p>この書生というのは時々我々を捕えて煮て食うという話である。<a href="https://example.com/neko">参考リンク</a>を参照。</p>
<h2 id="s1">第一節　書生</h2>
<p>しかしその当時は何という考もなかったから別段恐しいとも思わなかった。ただ彼の掌に載せられてスーと持ち上げられた時何だかフワフワした感じがあったばかりである。</p>
<p>掌の上で少し落ちついて書生の顔を見たのがいわゆる人間というものの見始であろう。この時妙なものだと思った感じが今でも残っている。</p>
<p>第一毛をもって装飾されべきはずの顔がつるつるしてまるで薬缶だ。その後猫にもだいぶ逢ったがこんな片輪には一度も出会わした事がない。のみならず顔の真中があまりに突起している。</p>
<figure><img src="{img}" width="90" height="120" alt="挿絵"/></figure>
<p>そうしてその穴の中から時々ぷうぷうと煙を吹く。どうも咽せぽくて実に弱った。これが人間の飲む煙草というものである事はようやくこの頃知った。</p>
<p>この書生の掌の裏でしばらくはよい心持に坐っておったが、しばらくすると非常な速力で運転し始めた。書生が動くのか自分だけが動くのか分らないが無暗に眼が廻る。胸が悪くなる。到底助からないと思っていると、どさりと音がして眼から火が出た。それまでは記憶しているがあとは何の事やらいくら考え出そうとしても分らない。</p>
<h1 id="c2">第二章　再会</h1>
<p>ふと気が付いて見ると書生はいない。たくさんおった兄弟が一疋も見えぬ。肝心の母親さえ姿を隠してしまった。その上今までの所とは違って無暗に明るい。眼を明いていられぬくらいだ。<a href="#c1">第一章へ戻る</a>。</p>
<p class="center">（了）</p>
</body></html>"""


def zh_tw_horizontal() -> str:
    css = """
@page { size: 148mm 210mm; margin: 15mm; }
html { font-family: "Noto Serif CJK TC", "WenQuanYi Zen Hei", "IPAGothic", serif; font-size: 12pt; line-height: 1.8; }
h1 { font-size: 20pt; }
h2 { font-size: 15pt; }
p { text-indent: 2em; margin: 0; }
rt { font-size: 6pt; }
"""
    return f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="utf-8"><title>臺灣的山</title><style>{css}</style></head><body>
<h1 id="c1">第一章　山的呼喚</h1>
<p>臺灣是一個多山的島嶼，全島有超過兩百座三千公尺以上的高山。這些山脈從北到南縱貫全島，形成了獨特的地形與氣候。登山者常說，山不會主動走向你，只有你走向山。</p>
<p>玉山是東北亞最高峰，海拔三千九百五十二公尺。<ruby>玉<rt>ㄩˋ</rt></ruby><ruby>山<rt>ㄕㄢ</rt></ruby>的名字來自冬季山頂積雪，遠望如玉。每年都有無數登山者前來朝聖，只為在山頂迎接日出。</p>
<h2 id="s1">第一節　登山準備</h2>
<p>登山前必須做好充分的準備，包括體能訓練、裝備檢查與路線規劃。高山的天氣變化莫測，早晨晴朗，午後可能就會下起大雨。攜帶雨具與保暖衣物是基本常識。</p>
<p>此外，申請入山證與入園證也是必要的手續，這些規定是為了保護山林生態，也是為了登山者自身的安全著想。</p>
</body></html>"""


def ko_horizontal() -> str:
    css = """
@page { size: 148mm 210mm; margin: 15mm; }
html { font-family: "Noto Serif CJK KR", "WenQuanYi Zen Hei", "IPAGothic", serif; font-size: 12pt; line-height: 1.8; }
h1 { font-size: 20pt; }
p { margin: 0 0 8pt 0; }
"""
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>한국의 사계절</title><style>{css}</style></head><body>
<h1 id="c1">제1장 봄</h1>
<p>한국의 봄은 삼월에 시작된다. 겨울 동안 얼어 있던 땅이 녹고, 산과 들에는 진달래와 개나리가 피어난다. 사람들은 따뜻한 햇살을 따라 밖으로 나가 산책을 즐긴다.</p>
<p>봄에는 황사가 찾아오기도 한다. 중국 북부의 사막에서 날아온 모래 먼지가 하늘을 뿌옇게 만든다. 그래서 봄철에는 마스크를 쓰는 사람이 많다.</p>
<h1 id="c2">제2장 여름</h1>
<p>여름은 덥고 습하다. 장마철에는 며칠씩 비가 계속 내리기도 한다. 하지만 장마가 끝나면 푸른 하늘과 뜨거운 태양이 돌아온다.</p>
</body></html>"""


FIXTURES = {
    "en_book": en_book,
    "ja_vertical": ja_vertical,
    "zh_tw_horizontal": zh_tw_horizontal,
    "ko_horizontal": ko_horizontal,
}


def find_chromium(explicit: str | None) -> str | None:
    candidates = [explicit, os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium",
                  "chromium", "chromium-browser", "google-chrome", "chrome"]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if p.is_dir():
            for sub in ("chrome", "chrome-linux/chrome", "chromium"):
                if (p / sub).exists():
                    return str(p / sub)
            continue
        found = shutil.which(c) or (c if p.exists() else None)
        if found:
            return found
    return None


def build(chromium: str, name: str, html: str, out_dir: Path) -> Path:
    src = out_dir / f"{name}.html"
    pdf = out_dir / f"{name}.pdf"
    src.write_text(html, encoding="utf-8")
    cmd = [chromium, "--headless=new", "--no-sandbox", "--disable-gpu",
           "--run-all-compositor-stages-before-draw", "--no-pdf-header-footer",
           "--generate-pdf-document-outline", f"--print-to-pdf={pdf}", src.as_uri()]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    src.unlink()
    return pdf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chromium")
    ap.add_argument("--out", default=str(HERE))
    args = ap.parse_args()
    chromium = find_chromium(args.chromium)
    if not chromium:
        print("No Chromium binary found; pass --chromium", file=sys.stderr)
        return 1
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in FIXTURES.items():
        pdf = build(chromium, name, fn(), out_dir)
        print(f"{pdf} ({pdf.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
