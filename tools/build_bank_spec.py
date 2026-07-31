from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Mm, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUT_DOCX = ROOT / "output" / "docx" / "ツールバコ_システム仕様書_銀行提出用_ONE_20260722.docx"
TMP = ROOT / "tmp" / "bank_spec"
LOGO = ROOT / "static" / "brand" / "toolbako-logo-header.png"
ARCH_DIAGRAM = TMP / "architecture.png"
FUNDS_DIAGRAM = TMP / "funds_flow.png"

# Design preset: standard_business_brief + memo_masthead.
# Named override JP_BANK_A4: A4 portrait, 20 mm side/top and 18 mm bottom margins.
# Named override TOOLBAKO_ACCENT: restrained brand navy/blue for headings and diagrams.
# Use the installed face name, not only the family name. LibreOffice on macOS
# otherwise substitutes a Latin-only face and renders Japanese as empty boxes.
FONT_LATIN = "Arial Unicode MS"
FONT_JA = "Arial Unicode MS"
NAVY = "183153"
BLUE = "356DE8"
CYAN = "EAF5FF"
LIGHT_BLUE = "EEF4FF"
LIGHT_GRAY = "F2F4F7"
MID_GRAY = "D8DEE8"
MUTED = "5C6675"
INK = "202733"
GOLD = "8A6116"
LIGHT_GOLD = "FFF6DE"
RED = "9B1C1C"
LIGHT_RED = "FDECEC"
GREEN = "1F6B45"
LIGHT_GREEN = "EAF7F0"
TABLE_WIDTH_DXA = 9360
TABLE_INDENT_DXA = 120


def rgb(hex_value: str) -> RGBColor:
    return RGBColor.from_string(hex_value)


def set_run_font(run, *, size: float | None = None, bold: bool | None = None,
                 color: str | None = None, italic: bool | None = None) -> None:
    run.font.name = FONT_LATIN
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), FONT_LATIN)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), FONT_LATIN)
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT_JA)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if color is not None:
        run.font.color.rgb = rgb(color)


def set_cell_shading(cell, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def set_cell_margins(cell, top: int = 90, start: int = 120, bottom: int = 90, end: int = 120) -> None:
    tc = cell._tc
    tc_pr = tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        tag = "w:" + edge
        node = tc_mar.find(qn(tag))
        if node is None:
            node = OxmlElement(tag)
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_cell_width(cell, width_dxa: int) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_w = tc_pr.find(qn("w:tcW"))
    if tc_w is None:
        tc_w = OxmlElement("w:tcW")
        tc_pr.append(tc_w)
    tc_w.set(qn("w:w"), str(width_dxa))
    tc_w.set(qn("w:type"), "dxa")


def configure_table(table, widths_dxa: Sequence[int], *, header: bool = True) -> None:
    if sum(widths_dxa) != TABLE_WIDTH_DXA:
        raise ValueError(f"table widths must total {TABLE_WIDTH_DXA}: {widths_dxa}")
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(TABLE_WIDTH_DXA))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_ind = tbl_pr.find(qn("w:tblInd"))
    if tbl_ind is None:
        tbl_ind = OxmlElement("w:tblInd")
        tbl_pr.append(tbl_ind)
    tbl_ind.set(qn("w:w"), str(TABLE_INDENT_DXA))
    tbl_ind.set(qn("w:type"), "dxa")
    layout = tbl_pr.find(qn("w:tblLayout"))
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        tbl_pr.append(layout)
    layout.set(qn("w:type"), "fixed")

    grid = table._tbl.tblGrid
    for child in list(grid):
        grid.remove(child)
    for width in widths_dxa:
        grid_col = OxmlElement("w:gridCol")
        grid_col.set(qn("w:w"), str(width))
        grid.append(grid_col)

    for row_idx, row in enumerate(table.rows):
        if header and row_idx == 0:
            tr_pr = row._tr.get_or_add_trPr()
            repeat = OxmlElement("w:tblHeader")
            repeat.set(qn("w:val"), "true")
            tr_pr.append(repeat)
        tr_pr = row._tr.get_or_add_trPr()
        cant_split = OxmlElement("w:cantSplit")
        tr_pr.append(cant_split)
        for idx, cell in enumerate(row.cells):
            set_cell_width(cell, widths_dxa[idx])
            set_cell_margins(cell)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER


def style_table_text(table, *, header: bool = True, compact: bool = False) -> None:
    for row_idx, row in enumerate(table.rows):
        for cell in row.cells:
            if header and row_idx == 0:
                set_cell_shading(cell, LIGHT_GRAY)
            for p in cell.paragraphs:
                p.paragraph_format.space_before = Pt(0)
                p.paragraph_format.space_after = Pt(2 if compact else 3)
                p.paragraph_format.line_spacing = 1.05
                for run in p.runs:
                    set_run_font(run, size=8.8 if compact else 9.2,
                                 bold=(header and row_idx == 0), color=INK)


def add_table(doc: Document, headers: Sequence[str], rows: Sequence[Sequence[str]],
              widths_dxa: Sequence[int], *, compact: bool = False):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    for i, text in enumerate(headers):
        table.rows[0].cells[i].text = text
    for row_values in rows:
        cells = table.add_row().cells
        for i, value in enumerate(row_values):
            cells[i].text = str(value)
    configure_table(table, widths_dxa, header=True)
    style_table_text(table, header=True, compact=compact)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return table


def add_kv_table(doc: Document, rows: Sequence[tuple[str, str]], *, label_width: int = 2300):
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = label
        cells[1].text = value
        set_cell_shading(cells[0], LIGHT_GRAY)
    configure_table(table, [label_width, TABLE_WIDTH_DXA - label_width], header=False)
    style_table_text(table, header=False)
    for row in table.rows:
        for run in row.cells[0].paragraphs[0].runs:
            set_run_font(run, size=9.2, bold=True, color=NAVY)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)
    return table


def add_callout(doc: Document, label: str, text: str, *, fill: str = LIGHT_BLUE,
                accent: str = BLUE) -> None:
    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    configure_table(table, [TABLE_WIDTH_DXA], header=False)
    cell = table.cell(0, 0)
    set_cell_shading(cell, fill)
    p = cell.paragraphs[0]
    p.paragraph_format.space_after = Pt(3)
    r = p.add_run(label + "  ")
    set_run_font(r, size=10, bold=True, color=accent)
    r = p.add_run(text)
    set_run_font(r, size=9.8, color=INK)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def add_paragraph(doc: Document, text: str, *, bold_lead: str | None = None,
                  align=WD_ALIGN_PARAGRAPH.LEFT, after: float = 6,
                  color: str = INK, size: float = 11) -> None:
    p = doc.add_paragraph()
    p.alignment = align
    p.paragraph_format.space_after = Pt(after)
    p.paragraph_format.line_spacing = 1.10
    if bold_lead and text.startswith(bold_lead):
        r = p.add_run(bold_lead)
        set_run_font(r, size=size, bold=True, color=color)
        r = p.add_run(text[len(bold_lead):])
        set_run_font(r, size=size, color=color)
    else:
        r = p.add_run(text)
        set_run_font(r, size=size, color=color)


def add_bullets(doc: Document, items: Iterable[str], *, level: int = 0) -> None:
    for item in items:
        p = doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
        p.paragraph_format.left_indent = Inches(0.5 if level == 0 else 0.75)
        p.paragraph_format.first_line_indent = Inches(-0.25)
        p.paragraph_format.space_after = Pt(4)
        p.paragraph_format.line_spacing = 1.10
        p.paragraph_format.keep_together = True
        r = p.add_run(item)
        set_run_font(r, size=10.5, color=INK)


def new_decimal_numbering_id(doc: Document) -> int:
    """Create an independent single-level decimal list that restarts at 1."""
    numbering = doc.part.numbering_part.element

    abstract_ids = [
        int(node.get(qn("w:abstractNumId")))
        for node in numbering.findall(qn("w:abstractNum"))
        if node.get(qn("w:abstractNumId")) is not None
    ]
    num_ids = [
        int(node.get(qn("w:numId")))
        for node in numbering.findall(qn("w:num"))
        if node.get(qn("w:numId")) is not None
    ]
    abstract_id = max(abstract_ids, default=-1) + 1
    num_id = max(num_ids, default=0) + 1

    abstract_num = OxmlElement("w:abstractNum")
    abstract_num.set(qn("w:abstractNumId"), str(abstract_id))
    multi_level_type = OxmlElement("w:multiLevelType")
    multi_level_type.set(qn("w:val"), "singleLevel")
    abstract_num.append(multi_level_type)

    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    num_fmt = OxmlElement("w:numFmt")
    num_fmt.set(qn("w:val"), "decimal")
    level_text = OxmlElement("w:lvlText")
    level_text.set(qn("w:val"), "%1.")
    level_jc = OxmlElement("w:lvlJc")
    level_jc.set(qn("w:val"), "left")
    level.extend([start, num_fmt, level_text, level_jc])

    paragraph_properties = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "720")
    tabs.append(tab)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "720")
    indent.set(qn("w:hanging"), "360")
    paragraph_properties.extend([tabs, indent])
    level.append(paragraph_properties)
    abstract_num.append(level)

    first_num = numbering.find(qn("w:num"))
    if first_num is None:
        numbering.append(abstract_num)
    else:
        numbering.insert(numbering.index(first_num), abstract_num)

    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(num_id))
    abstract_num_id = OxmlElement("w:abstractNumId")
    abstract_num_id.set(qn("w:val"), str(abstract_id))
    num.append(abstract_num_id)
    numbering.append(num)
    return num_id


def add_numbered(doc: Document, items: Iterable[str]) -> None:
    num_id = new_decimal_numbering_id(doc)
    for item in items:
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.5)
        p.paragraph_format.first_line_indent = Inches(-0.25)
        p.paragraph_format.space_after = Pt(5)
        p.paragraph_format.line_spacing = 1.10
        p.paragraph_format.keep_together = True
        num_properties = p._p.get_or_add_pPr().get_or_add_numPr()
        level = OxmlElement("w:ilvl")
        level.set(qn("w:val"), "0")
        number_id = OxmlElement("w:numId")
        number_id.set(qn("w:val"), str(num_id))
        num_properties.extend([level, number_id])
        r = p.add_run(item)
        set_run_font(r, size=10.5, color=INK)


def add_page_field(paragraph) -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend([begin, instr, separate, text, end])
    set_run_font(run, size=8.5, color=MUTED)


def style_document(doc: Document) -> None:
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(20)
    section.bottom_margin = Mm(18)
    section.left_margin = Mm(20)
    section.right_margin = Mm(20)
    section.header_distance = Mm(10)
    section.footer_distance = Mm(10)

    normal = doc.styles["Normal"]
    normal.font.name = FONT_LATIN
    normal._element.rPr.rFonts.set(qn("w:ascii"), FONT_LATIN)
    normal._element.rPr.rFonts.set(qn("w:hAnsi"), FONT_LATIN)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_JA)
    normal.font.size = Pt(11)
    normal.font.color.rgb = rgb(INK)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.10
    normal.paragraph_format.widow_control = True

    heading_specs = {
        "Heading 1": (16, NAVY, 16, 8),
        "Heading 2": (13, BLUE, 12, 6),
        "Heading 3": (12, NAVY, 8, 4),
    }
    for name, (size, color, before, after) in heading_specs.items():
        style = doc.styles[name]
        style.font.name = FONT_LATIN
        style._element.rPr.rFonts.set(qn("w:ascii"), FONT_LATIN)
        style._element.rPr.rFonts.set(qn("w:hAnsi"), FONT_LATIN)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_JA)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = rgb(color)
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.widow_control = True

    for list_name in ("List Bullet", "List Bullet 2", "List Number"):
        style = doc.styles[list_name]
        style.font.name = FONT_LATIN
        style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_JA)
        style.font.size = Pt(10.5)

    header = section.header
    p = header.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run("合同会社ONE  |  ツールバコ システム仕様書  |  銀行提出用")
    set_run_font(r, size=8.5, bold=True, color=MUTED)

    footer = section.footer
    table = footer.add_table(rows=1, cols=2, width=Inches(6.5))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    for cell in table.rows[0].cells:
        set_cell_margins(cell, top=0, start=0, bottom=0, end=0)
    left = table.cell(0, 0).paragraphs[0]
    left.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = left.add_run("提出版 1.1  |  2026年7月22日")
    set_run_font(r, size=8.5, color=MUTED)
    right = table.cell(0, 1).paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r = right.add_run("Page ")
    set_run_font(r, size=8.5, color=MUTED)
    add_page_field(right)


def find_font() -> str:
    candidates = list(Path("/System/Library/Fonts").glob("*角コ*W3.ttc"))
    candidates += [Path("/System/Library/Fonts/Hiragino Sans GB.ttc")]
    for path in candidates:
        if path.exists():
            return str(path)
    raise FileNotFoundError("Japanese font not found")


def draw_arrow(draw: ImageDraw.ImageDraw, start: tuple[int, int], end: tuple[int, int],
               color: str = "#6B778C", width: int = 6) -> None:
    draw.line([start, end], fill=color, width=width)
    x2, y2 = end
    x1, y1 = start
    dx, dy = x2 - x1, y2 - y1
    length = max((dx * dx + dy * dy) ** 0.5, 1)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    size = 16
    p1 = (x2, y2)
    p2 = (int(x2 - ux * size + px * size * 0.55), int(y2 - uy * size + py * size * 0.55))
    p3 = (int(x2 - ux * size - px * size * 0.55), int(y2 - uy * size - py * size * 0.55))
    draw.polygon([p1, p2, p3], fill=color)


def rounded_box(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str,
                lines: Sequence[str], title_font, body_font, fill: str, outline: str = "#B7C4D8") -> None:
    draw.rounded_rectangle(box, radius=22, fill=fill, outline=outline, width=3)
    x1, y1, x2, _ = box
    draw.text(((x1 + x2) / 2, y1 + 26), title, font=title_font, fill="#183153", anchor="ma")
    y = y1 + 78
    for line in lines:
        draw.text(((x1 + x2) / 2, y), line, font=body_font, fill="#39465A", anchor="ma")
        y += 38


def make_diagrams() -> None:
    TMP.mkdir(parents=True, exist_ok=True)
    font_path = find_font()
    title_font = ImageFont.truetype(font_path, 34)
    body_font = ImageFont.truetype(font_path, 25)
    small_font = ImageFont.truetype(font_path, 22)

    img = Image.new("RGB", (1600, 860), "white")
    draw = ImageDraw.Draw(img)
    draw.text((800, 45), "本番想定システム構成", font=title_font, fill="#183153", anchor="ma")
    rounded_box(draw, (80, 165, 360, 365), "利用者", ["購入者", "販売者", "運営管理者"], title_font, body_font, "#EEF4FF")
    rounded_box(draw, (480, 120, 920, 420), "ツールバコ Webアプリ", ["FastAPI / Jinja2 SSR", "Railway上で稼働", "認証・取引・運営画面"], title_font, body_font, "#EAF5FF", "#4E84E6")
    rounded_box(draw, (1080, 80, 1510, 270), "Supabase", ["Auth / PostgreSQL", "Storage / RLS"], title_font, body_font, "#EAF7F0", "#58A77B")
    rounded_box(draw, (1080, 330, 1510, 515), "Stripe", ["Connect / Checkout", "Identity / Webhook"], title_font, body_font, "#F1EDFF", "#7E64D1")
    rounded_box(draw, (480, 580, 920, 770), "運用サービス", ["メール送信 / Sentry", "ClamAV / バックアップ"], title_font, body_font, "#FFF6DE", "#C99A3D")
    draw_arrow(draw, (360, 265), (480, 265))
    draw_arrow(draw, (920, 215), (1080, 175))
    draw_arrow(draw, (920, 315), (1080, 420))
    draw_arrow(draw, (700, 420), (700, 580))
    draw.text((420, 230), "HTTPS", font=small_font, fill="#5C6675", anchor="ma")
    draw.text((1000, 160), "認証・データ", font=small_font, fill="#5C6675", anchor="ma")
    draw.text((1005, 365), "決済・本人確認", font=small_font, fill="#5C6675", anchor="ma")
    img.save(ARCH_DIAGRAM, quality=94)

    img = Image.new("RGB", (1600, 860), "white")
    draw = ImageDraw.Draw(img)
    draw.text((800, 45), "通常購入時の決済・売上分配フロー", font=title_font, fill="#183153", anchor="ma")
    rounded_box(draw, (70, 175, 370, 380), "購入者", ["商品・条件を確認", "カード等で支払"], title_font, body_font, "#EEF4FF")
    rounded_box(draw, (490, 135, 920, 420), "Stripe Connect", ["決済処理", "本人確認・不正対策", "売上分配・返金"], title_font, body_font, "#F1EDFF", "#7E64D1")
    rounded_box(draw, (1120, 90, 1520, 290), "販売者", ["納品・サポート", "売上の受取"], title_font, body_font, "#EAF7F0", "#58A77B")
    rounded_box(draw, (1120, 365, 1520, 555), "合同会社ONE", ["運営・審査・サポート", "販売手数料 10%予定"], title_font, body_font, "#FFF6DE", "#C99A3D")
    rounded_box(draw, (490, 610, 920, 785), "ツールバコ", ["Webhookで入金結果を確認", "取引状態・書類・通知を更新"], title_font, body_font, "#EAF5FF", "#4E84E6")
    draw_arrow(draw, (370, 275), (490, 275))
    draw_arrow(draw, (920, 225), (1120, 190))
    draw_arrow(draw, (920, 330), (1120, 455))
    draw_arrow(draw, (705, 420), (705, 610))
    draw.text((430, 235), "購入代金", font=small_font, fill="#5C6675", anchor="ma")
    draw.text((1010, 160), "販売者受取分", font=small_font, fill="#5C6675", anchor="ma")
    draw.text((1025, 390), "プラットフォーム手数料", font=small_font, fill="#5C6675", anchor="ma")
    draw.text((800, 835), "カード番号等はStripeが管理し、ツールバコのアプリケーションでは保持しない設計", font=small_font, fill="#5C6675", anchor="ms")
    img.save(FUNDS_DIAGRAM, quality=94)


def set_picture_alt(inline_shape, title: str, description: str) -> None:
    doc_pr = inline_shape._inline.docPr
    doc_pr.set("title", title)
    doc_pr.set("descr", description)


def add_picture(doc: Document, path: Path, width_inches: float, title: str, description: str) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(8)
    shape = p.add_run().add_picture(str(path), width=Inches(width_inches))
    set_picture_alt(shape, title, description)


def heading(doc: Document, text: str, level: int = 1, *, page_break: bool = False) -> None:
    p = doc.add_heading(text, level=level)
    if page_break:
        p.paragraph_format.page_break_before = True


def build_document() -> Document:
    make_diagrams()
    doc = Document()
    style_document(doc)
    doc.core_properties.title = "ツールバコ システム仕様書（銀行提出用）"
    doc.core_properties.subject = "AIツール売買・マッチングプラットフォームのシステムおよび取引仕様"
    doc.core_properties.keywords = "ツールバコ, 合同会社ONE, AIツール, マーケットプレイス, システム仕様書"
    doc.core_properties.created = datetime(2026, 7, 22, 0, 0, 0)
    doc.core_properties.modified = datetime(2026, 7, 22, 0, 0, 0)

    # Cover - memo_masthead adapted for an external bank submission.
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(22)
    p.paragraph_format.space_after = Pt(24)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    shape = p.add_run().add_picture(str(LOGO), width=Inches(3.3))
    set_picture_alt(shape, "ツールバコ ロゴ", "AIツールの個人売買マーケット ツールバコのロゴ")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(8)
    r = p.add_run("システム仕様書")
    set_run_font(r, size=28, bold=True, color=NAVY)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(28)
    r = p.add_run("AIツール売買・マッチングプラットフォーム「ツールバコ」")
    set_run_font(r, size=14, bold=True, color=BLUE)

    add_kv_table(doc, [
        ("提出目的", "金融機関に対するサービス内容、取引フロー、決済方式および安全管理体制の説明"),
        ("運営事業者", "合同会社ONE"),
        ("文書区分", "銀行提出用 / 提出版"),
        ("版・作成日", "第1.1版 / 2026年7月22日"),
        ("対象システム", "ツールバコ Webアプリケーション"),
    ], label_width=2200)
    add_callout(doc, "現在の稼働状態", "開発・検証環境です。デモ決済のみで、実際の請求、本人確認書類の受領、販売者への送金は行っていません。実取引は本書記載の本番移行条件を満たした後に開始します。", fill=LIGHT_GOLD, accent=GOLD)

    doc.add_page_break()
    heading(doc, "文書管理")
    add_table(doc, ["版", "日付", "区分", "内容"], [
        ["1.0", "2026年7月16日", "初版", "銀行提出用として、サービス・取引・技術・安全管理・本番移行条件を整理"],
        ["1.1", "2026年7月22日", "運営主体更新", "運営事業者を合同会社ONEへ変更し、売上・手数料・販売者振込管理を反映"],
    ], [1100, 1800, 1500, 4960])
    add_paragraph(doc, "本書は、2026年7月22日時点のソースコード、画面、データベース定義および運用計画に基づくシステム仕様説明資料です。利用規約、プライバシーポリシー、特定商取引法表示および各外部サービス契約が正式な運用条件を定めます。")

    heading(doc, "銀行確認用サマリー")
    add_kv_table(doc, [
        ("事業内容", "個人開発者等が作成したAIツールを、個人・個人事業主・小規模事業者等へ紹介、相談、販売するオンラインマーケットプレイス"),
        ("取扱対象", "Webアプリ、ダウンロードツール、プログラム、プロンプト、業務自動化、カスタマイズ開発、ソースコード等の独占譲渡相談"),
        ("販売方式", "通常購入、カスタマイズ見積もり、独占譲渡相談の3方式"),
        ("決済予定", "Stripe Checkout / Stripe Connect。カード情報はStripeが管理し、本システムは保持しない"),
        ("手数料予定", "通常購入およびカスタマイズ取引の販売価格に対し10%（正式条件は公開前に利用規約・画面へ明示）"),
        ("独自ウォレット", "提供しない。任意の入出金、送金、換金、ポイント、暗号資産の機能は設けない"),
        ("本人確認予定", "Supabase Authによるログイン認証、Stripe Identityによる本人確認。高額・独占譲渡・販売者受取等で確認を要求"),
        ("現在の状態", "開発・検証中。実決済、実送金、独占譲渡の電子契約・エスクローは未稼働"),
    ], label_width=2200)

    heading(doc, "1. サービスおよび運営主体", page_break=True)
    heading(doc, "1.1 サービスの目的", level=2)
    add_paragraph(doc, "ツールバコは、AIを活用して個人や小規模チームが開発したツールを、必要とする個人・個人事業主・小規模事業者へ届けるためのオンラインマーケットプレイスです。従来は企業向けシステムが高額になりやすかった領域で、目的、価格、利用条件、開発者の実績を比較し、購入前に相談できる取引環境を提供します。")
    add_bullets(doc, [
        "出品者が自ら価格、提供方式、ライセンス、納期、サポート範囲を表示する。",
        "購入者が検索、比較、口コミ、プロフィール、購入前相談を通じて選択する。",
        "運営者が認証、決済連携、取引記録、通報、紛争受付、安全審査の基盤を提供する。",
        "AIツール特有の動作環境、利用AI、API費用、バージョン、権利関係を明示する。",
    ])

    heading(doc, "1.2 運営事業者", level=2)
    add_kv_table(doc, [
        ("事業者名", "合同会社ONE"),
        ("運営責任者", "正式公開前に確定"),
        ("所在地", "正式公開前に確定"),
        ("電話番号", "正式公開前に確定"),
        ("問い合わせ先", "正式公開前に確定"),
        ("会社Webサイト", "正式公開前に確定"),
    ], label_width=2200)
    add_paragraph(doc, "合同会社ONEの登記事項・公開窓口を確認後、運営責任者、所在地、電話番号、問い合わせ先および会社Webサイトを本番設定・特定商取引法表示・本書へ同一内容で反映します。未確認のCRK情報は流用しません。適格請求書発行事業者番号は確認済みの場合のみ掲載します。", size=9.5, color=MUTED)

    heading(doc, "1.3 サービスの位置づけ", level=2)
    add_paragraph(doc, "当社は、出品者と購入者がAIツール等を取引するための場、取引画面および運営サポートを提供します。個別商品の販売主体、商品内容、知的財産権、利用許諾および納品義務は原則として出品者が負い、当社は規約に基づき出品審査、決済連携、記録、問い合わせおよび紛争対応を行います。")

    heading(doc, "2. 利用者と取扱商品")
    heading(doc, "2.1 利用者区分", level=2)
    add_table(doc, ["区分", "主な利用目的", "主な権限"], [
        ["閲覧者", "商品・開発者の確認", "検索、一覧、詳細、レビュー等の閲覧"],
        ["購入者", "ツール購入・導入相談", "購入、相談、見積もり依頼、納品確認、評価、紛争申請"],
        ["販売者", "ツール・開発サービスの販売", "出品、価格設定、提案、納品、売上・振込管理"],
        ["運営管理者", "安全・取引・問い合わせ管理", "出品審査、非公開化、通報、本人確認状態、返金・紛争対応"],
    ], [1500, 3000, 4860])

    heading(doc, "2.2 取扱商品", level=2)
    add_bullets(doc, [
        "Webアプリケーション、業務自動化ツール、データ分析ツール。",
        "ダウンロード形式のプログラム、テンプレート、設定ファイル、ワークフロー。",
        "AIプロンプト、文章・画像生成支援、マーケティング・教育・開発支援ツール。",
        "購入者の業務に合わせるカスタマイズ開発、導入支援、保守相談。",
        "完成済みサービスのソースコード、運用手順、ブランド資産等の独占譲渡相談。",
    ])
    heading(doc, "2.3 取扱禁止・制限", level=2)
    add_paragraph(doc, "マルウェア、認証情報の窃取、違法なスクレイピング、第三者の知的財産権を侵害する商品、販売権限のない転売、虚偽表示、詐欺・差別・違法行為を助長する商品を禁止します。独占譲渡で顧客情報や外部アカウントを含める場合は、法令、契約、本人同意および移管可否の確認を必須とし、NDA前の機密情報入力を制限します。")

    heading(doc, "3. 販売方式と取引フロー", page_break=True)
    heading(doc, "3.1 3つの販売方式", level=2)
    add_table(doc, ["販売方式", "対象", "主な流れ", "本番範囲"], [
        ["通常購入", "同一ツールを複数購入者へ提供", "商品確認 → 購入 → 決済 → 即時提供または納品 → 評価", "Stripe接続後に稼働"],
        ["カスタマイズ", "購入者の業務に合わせた修正・開発", "相談 → 要件確認 → 見積もり提案 → 購入 → 納品・検収", "Stripe接続後に稼働"],
        ["独占譲渡", "ソースコード・権利・運用資産の一括譲渡", "問い合わせ → 本人確認 → 双方NDA → 交渉 → 外部契約・決済", "問い合わせ・NDAまで。契約・入金は未稼働"],
    ], [1500, 2200, 3660, 2000], compact=True)

    heading(doc, "3.2 通常購入", level=2)
    add_numbered(doc, [
        "購入者が商品詳細、価格、動作環境、ライセンス、納品形式、サポート期間、キャンセル条件を確認する。",
        "購入者が買い切りまたは対応商品では月額方式を選択し、オプション・クーポンを反映する。",
        "本番ではStripe Checkoutへ遷移し、決済結果を署名Webhookで確認する。",
        "即時提供商品はライブラリへ利用情報を付与し、制作・調整型商品は取引ルームを開始する。",
        "納品、承諾、差し戻し、追加支払い、キャンセル申請を取引ルームで記録する。",
        "取引完了後、購入者・販売者が相互評価し、販売実績へ反映する。",
    ])

    heading(doc, "3.3 カスタマイズ取引", level=2)
    add_paragraph(doc, "一般質問とは別の相談スレッドを作成し、用途、現状業務、予算、納期等を確認します。販売者は提案タイトル、金額、納期、提案内容を提示し、購入者が承諾した提案のみ購入へ進みます。納品後の修正回数、追加支払い、検収、評価を取引記録として保持します。")

    heading(doc, "3.4 独占譲渡相談", level=2)
    add_numbered(doc, [
        "販売者が希望価格、月間売上・利益・維持費、運営時間、技術構成、譲渡対象、引き継ぎ期間を登録する。",
        "運営が独占譲渡向けの掲載審査を行い、承認された案件のみ公開する。",
        "購入希望者が希望金額、利用目的、相談内容を送信する。",
        "当事者の本人確認後、案件単位のNDAを双方が確認し、詳細情報を開示する。",
        "契約書、デューデリジェンス、エスクロー、権利移転、引き継ぎは外部サービス・専門家の確認後に実装する。現時点ではサイト内で契約成立・入金完了と表示しない。",
    ])

    heading(doc, "4. 決済・売上分配・返金")
    add_picture(doc, FUNDS_DIAGRAM, 6.45, "決済・売上分配フロー", "購入者、Stripe Connect、販売者、合同会社ONE、ツールバコ間の決済とWebhookの流れ")
    heading(doc, "4.1 決済方式", level=2)
    add_bullets(doc, [
        "本番決済はStripe Checkoutを利用し、販売者受取はStripe Connectを利用する設計。",
        "カード番号、セキュリティコード等のカード情報はStripeの画面・基盤で処理し、本システムへ保存しない。",
        "決済成功、返金、異議申立て、定期課金、Connect口座状態、本人確認結果を署名付きWebhookで受け取る。",
        "同一イベントの二重処理を防ぐイベントID管理と、金額・通貨・注文・参照IDの照合を行う。",
    ])
    heading(doc, "4.2 販売手数料・売上", level=2)
    add_paragraph(doc, "現行実装の販売手数料は取引金額の10%です。販売者受取額は、取引金額から販売手数料、返金、異議申立て等の調整額を差し引いて算定します。正式な料率、最低振込額、振込時期、Stripe手数料の負担者は本番公開前に利用規約、特定商取引法表示および購入・販売画面へ明示します。")
    heading(doc, "4.3 キャンセル・返金・異議申立て", level=2)
    add_bullets(doc, [
        "キャンセルは取引ルームから申請し、当事者の合意または運営判断で処理する。",
        "納品済みデジタル商品の購入者都合返品は原則対象外とし、未納品、重大な説明相違、不正利用等は個別審査する。",
        "返金時はStripeの返金APIを使用し、販売者への移転分とプラットフォーム手数料の反映を連動させる。",
        "カード会社側の異議申立てはWebhookで取引を保留し、証憑・メッセージ・納品履歴を基に対応する。",
    ])
    add_callout(doc, "資金取扱い", "ユーザーが任意に資金を入出金・送金・換金する独自ウォレットは提供しません。購入代金は特定の商品・サービス取引に紐づき、資金移動はStripeの契約・審査・仕様に従います。")

    heading(doc, "5. 機能仕様")
    add_table(doc, ["機能群", "主な機能", "公開範囲"], [
        ["公開・検索", "トップ、一覧、カテゴリ、タグ、価格、AI、人気・新着・急上昇、比較", "ログイン不要"],
        ["商品詳細", "説明、画像、動作環境、AIツールパスポート、FAQ、レビュー、関連商品", "ログイン不要"],
        ["アカウント", "会員登録、ログイン、OAuth、パスワード再設定、プロフィール、退会予約", "本人のみ"],
        ["購入者", "購入、ライブラリ、購入履歴、取引ルーム、書類、評価、お気に入り", "本人・取引当事者"],
        ["販売者", "出品、編集、販売休止、バージョン、売上、クーポン、振込、分析", "販売者本人"],
        ["相談・提案", "購入前DM、カスタマイズ相談、見積もり提案、公開募集、応募比較", "当事者"],
        ["独占譲渡", "譲渡案件公開、問い合わせ、本人確認、案件NDA、交渉状態", "公開情報＋当事者限定"],
        ["安全・運営", "通報、ブロック、本人確認、審査、問い合わせ、紛争、監査、管理画面", "本人・管理者"],
        ["通知", "サイト内通知、カテゴリ別通知設定、更新フォロー、再開通知", "本人のみ"],
        ["SEO・共有", "SSR、OGP、構造化データ、sitemap、robots、X共有", "公開"],
    ], [1500, 5360, 2500], compact=True)

    heading(doc, "5.1 取引ステータス", level=2)
    add_table(doc, ["段階", "内容", "主な制御"], [
        ["決済待ち", "Checkout開始から入金確認まで", "Webhook確認前は納品・売上確定をしない"],
        ["進行中", "要件確認・制作・調整中", "当事者のみ取引ルーム閲覧"],
        ["納品済み", "販売者が成果物を提出", "購入者が承諾または差し戻し"],
        ["キャンセル申請", "一方が理由を提示", "合意または運営判断まで売上を確定しない"],
        ["完了", "検収・提供が完了", "レビュー・売上計上・書類発行"],
        ["紛争・返金", "問題申請またはカード異議申立て", "振込保留、証跡確認、結果記録"],
    ], [1500, 4300, 3560])

    heading(doc, "5.2 取引書類", level=2)
    add_paragraph(doc, "取引単位で見積書、発注書、納品書、領収書を表示・保存する機能を備えます。領収書には運営事業者情報、購入者、対象商品、金額、決済日、取引番号を表示し、適格請求書発行事業者番号は確認済みの場合のみ掲載します。")

    heading(doc, "6. システム構成")
    add_picture(doc, ARCH_DIAGRAM, 6.45, "本番想定システム構成", "利用者、ツールバコWebアプリ、Supabase、Stripe、運用サービスの接続関係")
    heading(doc, "6.1 技術構成", level=2)
    add_table(doc, ["区分", "採用技術・サービス", "用途"], [
        ["Webアプリ", "Python / FastAPI / Uvicorn", "リクエスト処理、業務ロジック、API、Webhook"],
        ["画面", "Jinja2 / HTML / CSS / Vanilla JavaScript", "SSRによる画面表示、SEO、操作"],
        ["本番DB", "Supabase PostgreSQL（予定）", "ユーザー、商品、注文、会話、審査、監査"],
        ["認証", "Supabase Auth（予定）", "メール、Google、X等のログイン認証"],
        ["ファイル", "Supabase Storage / 非公開領域（予定）", "商品画像、納品ファイル、期限付き配布"],
        ["決済", "Stripe Checkout / Connect / Identity（予定）", "購入、売上分配、本人確認、返金"],
        ["ホスティング", "Railway（予定）", "アプリ稼働、ヘルスチェック、自動再起動"],
        ["運用", "メール送信、Sentry、ClamAV（予定）", "通知、エラー監視、ファイル検査"],
    ], [1800, 3000, 4560])

    heading(doc, "6.2 現在の検証環境", level=2)
    add_paragraph(doc, "現在はデモ用のメモリ状態管理と、必要に応じた単一インスタンス向けSQLiteスナップショット保存を使用します。デモモードでは請求、送金、外部本人確認を行わず、入力された本人確認情報は保存しない設計です。Supabase向けデータベース定義、RLS、Stripe接続コードは作成済みですが、実決済開始前に本番PostgreSQL repositoryへ置換します。")

    heading(doc, "7. データ・認証・権限制御")
    heading(doc, "7.1 主なデータ分類", level=2)
    add_table(doc, ["データ分類", "例", "取扱方針"], [
        ["アカウント", "氏名表示、メール、プロフィール、認証状態", "本人・運営権限で制御。パスワードは認証基盤で管理"],
        ["商品", "説明、価格、画像、ライセンス、動作環境", "公開情報と非公開ファイルを分離"],
        ["取引", "注文、金額、状態、メッセージ、納品、レビュー", "購入者・販売者・運営のみ"],
        ["決済参照", "Stripeの注文・支払・返金・異議申立てID", "カード番号は保存せず参照IDのみ"],
        ["本人確認", "確認状態、外部サービス参照ID", "本人確認書類本体はStripe側で管理する設計"],
        ["運営記録", "通報、問い合わせ、紛争、監査ログ、同意履歴", "一般利用者から非公開。保持方針に従う"],
        ["ファイル", "商品画像、納品ZIP・PDF等", "非公開保管、権限・検査・期限付き提供"],
    ], [1700, 3400, 4260])

    heading(doc, "7.2 認証", level=2)
    add_bullets(doc, [
        "本番はSupabase Authを利用し、OAuthおよびメール認証を行う。",
        "OAuth認可コードはサーバー側PKCEで交換し、アクセストークンをURLへ残さない。",
        "ログイン後は推測困難なセッションIDを発行し、セッション本体をサーバー側で管理する。",
        "BAN、パスワード再設定、他端末ログアウト時にセッションを失効できる。",
        "管理者、販売者の振込・返金・権限操作は本番でAAL2相当のMFA・再認証を追加する。",
    ])

    heading(doc, "7.3 権限制御", level=2)
    add_paragraph(doc, "アプリケーション層で購入者、販売者、管理者、取引当事者を毎回確認します。SupabaseではRow Level Securityを有効化し、商品編集は作成者、注文・会話・納品は当事者、振込は本人確認済み販売者、監査ログ・Webhook記録はサーバー用権限のみに限定するデータベース方針です。独占譲渡案件は当事者以外へ存在を開示しません。")

    heading(doc, "8. セキュリティ仕様")
    add_table(doc, ["対策領域", "実装・設計内容", "状態"], [
        ["通信", "HTTPS、HSTS、許可ホスト、信頼プロキシ範囲の限定", "本番設定時に有効"],
        ["ブラウザ", "CSP nonce、frame拒否、MIME sniffing防止、Referrer制御", "実装済み"],
        ["不正リクエスト", "Origin/Referer検証、リクエストサイズ上限、レート制限", "単一インスタンス実装済み"],
        ["認証", "署名Cookie、サーバー側失効可能セッション、scrypt、OAuth PKCE", "実装済み / Supabase接続予定"],
        ["権限", "サーバー側当事者判定、管理者判定、RLS定義", "実装・定義済み"],
        ["決済", "Stripe署名検証、5分以内の時刻検証、重複イベント防止、金額・通貨照合", "実装済み / 本番E2E前"],
        ["アップロード", "50MB上限、許可拡張子、MIME署名、ZIP traversal・bomb・symlink検査", "実装済み"],
        ["マルウェア", "ClamAV連携、検査合格ファイルのみ配布", "接続先設定予定"],
        ["秘密情報", "環境変数・Secretsへ保存。service roleやStripe secretをブラウザへ返さない", "設計済み"],
        ["監査", "操作ログ、request ID、同意履歴、WebhookイベントID", "本番永続化強化前"],
    ], [1650, 5740, 1970], compact=True)
    add_callout(doc, "公開前の重要対策", "複数インスタンス共通のレート制限、MFA、追記専用監査ログ、Webhook outbox・再試行・dead-letterを本番公開条件とします。", fill=LIGHT_RED, accent=RED)

    heading(doc, "9. 運用・監視・事故対応", page_break=True)
    heading(doc, "9.1 通常運用", level=2)
    add_bullets(doc, [
        "アプリの稼働確認は /healthz、外部接続を含む公開準備確認は /readyz で行う。",
        "本番エラーはrequest IDを付与し、利用者へ内部情報を表示せず、Sentry等へ通知する。",
        "管理画面で通報、出品安全審査、本人確認状態、問い合わせ、紛争、アカウント停止を処理する。",
        "重要な取引・運営操作を監査ログへ記録し、決済・メール・Webhookの失敗を追跡する。",
        "バックアップ、復元手順、保持期間、運用責任者は本番公開前に文書化する。",
    ])

    heading(doc, "9.2 問い合わせ・紛争", level=2)
    add_paragraph(doc, "問い合わせはアカウント、決済、本人確認、商品、セキュリティ、その他に分類し、決済・紛争・セキュリティ案件を高優先度として扱います。取引ルームのメッセージ、提案、注文、納品、承諾、差し戻し、決済参照、監査記録を確認し、出品停止、売上保留、返金、アカウント制限等を判断します。")

    heading(doc, "9.3 障害・情報事故", level=2)
    add_numbered(doc, [
        "検知：監視アラート、利用者問い合わせ、決済Webhook失敗等から事象を検知する。",
        "封じ込め：該当機能、出品、アカウント、振込、Webhook処理を一時停止する。",
        "調査：request ID、監査ログ、決済事業者記録、DB変更履歴を確認する。",
        "復旧：原因修正、再処理、バックアップ復元、外部サービス連携の再確認を行う。",
        "報告：影響範囲を確定し、法令・契約・社内方針に基づき利用者、金融機関、外部事業者等へ連絡する。",
    ])

    heading(doc, "10. 法務・コンプライアンス方針")
    add_table(doc, ["項目", "システム上の対応", "公開前の確認"], [
        ["特定商取引法", "事業者名、責任者、住所、電話、価格、支払・提供・返品条件の表示", "専門家による最終確認"],
        ["利用規約", "取引の位置づけ、手数料、キャンセル、知的財産、禁止事項、利用停止", "料率・責任分界の確定"],
        ["プライバシー", "取得目的、外部委託、保存、開示・削除、Cookie等の説明", "外部サービスと保持期間の確定"],
        ["本人確認", "高額・販売者・独占譲渡で外部本人確認を要求", "審査基準・再確認条件の確定"],
        ["知的財産", "販売権限の表明、ライセンス表示、通報・非公開化", "侵害申告・反論手順の確定"],
        ["独占譲渡", "NDA、譲渡対象、事業指標、当事者限定交渉", "契約書、エスクロー、法務DD"],
        ["資金・決済", "Stripeを利用し独自ウォレットを設けない", "契約・審査・資金移動条件の確認"],
    ], [1800, 4380, 3180])
    add_paragraph(doc, "本書は技術・運用上の仕様を説明するものであり、法令該当性の最終判断を代替するものではありません。利用規約、プライバシーポリシー、手数料、返金、本人確認、独占譲渡契約は、本番公開前に弁護士・専門家と確認します。", size=9.5, color=MUTED)

    heading(doc, "11. テスト・品質状況")
    add_table(doc, ["確認項目", "結果", "備考"], [
        ["自動テスト", "79件合格", "認証、購入、取引、返金、Webhook、権限、境界値、運営売上管理等"],
        ["構文・テンプレート", "合格", "Python、JavaScript、Jinja2テンプレートを確認"],
        ["画面巡回", "重大エラーなし", "匿名・ログイン後の主要画面、画像、リンク、HTMLを確認"],
        ["権限テスト", "合格", "管理画面、他人の商品、取引・独占譲渡案件へのアクセスを拒否"],
        ["SEO", "合格", "タイトル、説明、canonical、構造化データ、sitemap、robots"],
        ["既知の軽微修正", "公開前に対応", "Cookieキャッシュ、HTMLランドマーク、フォームラベル、asset version"],
        ["実サービスE2E", "未実施", "Stripe、Supabase、メール、Storage、ClamAV接続後に実施"],
        ["負荷・CWV・復元", "未実施", "本番相当URL・DB・監視環境で実施"],
    ], [2300, 1800, 5260])
    add_paragraph(doc, "上記はローカルおよび検証環境での結果です。第三者による脆弱性診断、金融機関・決済事業者の審査、実データを用いた本番テストを意味するものではありません。", size=9.5, color=MUTED)

    heading(doc, "12. 本番移行条件と未稼働機能", page_break=True)
    heading(doc, "12.1 合同会社ONEが行う準備", level=2)
    add_bullets(doc, [
        "公開ドメイン、DNS、TLS、ホスティング、Secretsを設定する。",
        "Supabase本番プロジェクト、Auth、Storage、RLS、バックアップを設定する。",
        "Stripe法人審査、Connect、Checkout、Identity、入金口座、Webhookを本番有効化する。",
        "送信ドメインのSPF、DKIM、DMARCを設定し、取引メールを有効化する。",
        "利用規約、プライバシー、特商法、手数料、返金、禁止商品、紛争基準を確定する。",
        "問い合わせ、返金、通報、障害、個人情報事故の責任者・営業時間を決定する。",
    ])

    heading(doc, "12.2 実装・インフラ側の公開条件", level=2)
    add_bullets(doc, [
        "注文、在庫、Webhook、売上、監査を同一PostgreSQLトランザクションで扱うrepositoryへ置換する。",
        "Webhookのoutbox、再試行、dead-letter、イベント一意制約を実装する。",
        "RedisまたはエッジWAFで複数インスタンス共通のレート制限を実装する。",
        "Supabase AAL2を用いたMFAと、振込・返金・管理操作時の再認証を実装する。",
        "追記専用監査ログ、非公開Storage、マルウェア検査、期限付きURLを本番接続する。",
        "決済失敗、返金、異議申立て、定期更新失敗、重複Webhook、本人確認を含むE2Eを合格させる。",
        "想定ピークの2倍の負荷、Core Web Vitals、バックアップ復元を確認する。",
    ])

    heading(doc, "12.3 未稼働または外部確定待ちの機能", level=2)
    add_table(doc, ["機能", "現在", "稼働条件"], [
        ["実カード決済・売上分配", "接続コード実装済み、デモのみ", "Stripe本番審査・鍵・E2E"],
        ["外部本人確認", "接続コード実装済み、デモのみ", "Stripe Identity本番設定"],
        ["本番認証・DB・Storage", "SQL・RLS定義済み", "Supabase本番接続・repository置換"],
        ["取引メール", "送信分岐実装済み", "送信サービス・ドメイン認証"],
        ["マルウェア検査", "ClamAV接続実装済み", "本番スキャナー接続"],
        ["独占譲渡の電子契約", "未稼働", "契約サービス・契約書・法務確認"],
        ["独占譲渡のエスクロー", "未稼働", "提供事業者選定・審査・実装"],
        ["第三者による事業・権利DD", "未提供", "専門家・提供範囲の契約"],
    ], [2500, 3100, 3760])
    add_callout(doc, "公開判定", "上記条件を満たし、/readyz がHTTP 200かつ ready: true になるまでDEMO_MODEを解除せず、実決済、本人確認書類、実納品ファイルを受け付けません。", fill=LIGHT_GREEN, accent=GREEN)

    heading(doc, "13. 付録")
    heading(doc, "13.1 主要画面・URL", level=2)
    add_table(doc, ["区分", "代表URL", "内容"], [
        ["公開", "/, /tools, /tools/{slug}, /creators, /transfers", "トップ、検索、商品、開発者、独占譲渡"],
        ["認証", "/signup, /login, /auth/callback", "会員登録、ログイン、OAuth"],
        ["購入者", "/checkout/{slug}, /purchases, /library, /orders/{id}", "決済確認、購入履歴、提供物、取引"],
        ["販売者", "/tools/new, /seller, /seller/payments, /payouts", "出品、販売管理、受取設定、振込"],
        ["相談", "/messages, /requests, /transfer-inquiries/{id}", "DM、公開募集、独占譲渡案件"],
        ["安全", "/verification, /support, /security, /admin", "本人確認、問い合わせ、セキュリティ、運営"],
        ["法務", "/terms, /privacy, /tokushoho", "利用規約、プライバシー、特商法"],
        ["運用", "/healthz, /readyz, /webhooks/stripe", "稼働確認、公開準備、決済イベント"],
    ], [1500, 3860, 4000], compact=True)

    heading(doc, "13.2 本書の前提", level=2)
    add_bullets(doc, [
        "サービス名称は「ツールバコ」、運営者は合同会社ONEとして記載する。",
        "価格、手数料、外部事業者、運用体制は本番公開時の正式表示・契約を優先する。",
        "画面・機能は安全性、法令、決済事業者・金融機関の要請により変更する場合がある。",
        "本書に記載した『予定』『未稼働』『公開前』の項目は、実装・契約・審査の完了を保証する表現ではない。",
    ])

    heading(doc, "13.3 連絡先", level=2)
    add_kv_table(doc, [
        ("運営事業者", "合同会社ONE"),
        ("運営責任者", "松本憲吾"),
        ("問い合わせ", "正式公開前に確定"),
        ("会社Webサイト", "正式公開前に確定"),
    ], label_width=2200)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(18)
    p.paragraph_format.space_after = Pt(0)
    r = p.add_run("以上")
    set_run_font(r, size=11, bold=True, color=NAVY)

    return doc


def main() -> None:
    OUT_DOCX.parent.mkdir(parents=True, exist_ok=True)
    TMP.mkdir(parents=True, exist_ok=True)
    doc = build_document()
    doc.save(OUT_DOCX)
    print(OUT_DOCX)


if __name__ == "__main__":
    main()
