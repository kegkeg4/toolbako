from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OG_DIR = ROOT / "static" / "generated"
OG_DIR.mkdir(parents=True, exist_ok=True)


def _font(size: int, bold: bool = False):
    candidates = [
        ROOT / "assets" / ("NotoSansJP-Bold.ttf" if bold else "NotoSansJP-Regular.ttf"),
        Path("/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc" if bold else "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists(): return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _fit(draw, text: str, max_width: int, size: int, bold: bool = False):
    while size > 28:
        font = _font(size, bold)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width: return font
        size -= 2
    return _font(size, bold)


def generate_og(slug: str, name: str, tagline: str, author: str) -> str:
    path = OG_DIR / f"{slug}.jpg"
    if path.exists(): return f"/static/generated/{slug}.jpg"
    img = Image.new("RGB", (1200, 630), "#FFF8F3")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((54, 50, 1146, 580), radius=36, fill="#FFFFFF", outline="#E9DED6", width=3)
    d.rounded_rectangle((85, 85, 340, 545), radius=28, fill="#FF6B4A")
    d.ellipse((146, 190, 278, 322), fill="#FFF8F3")
    d.rounded_rectangle((178, 157, 246, 352), radius=28, fill="#FFF8F3")
    d.text((390, 105), "ツールバコ / NEW TOOL", font=_font(26, True), fill="#FF6B4A")
    title_font = _fit(d, name, 700, 72, True)
    d.text((390, 178), name, font=title_font, fill="#26221F")
    d.multiline_text((390, 305), tagline[:50], font=_font(32), fill="#625B55", spacing=10)
    d.text((390, 490), f"by @{author}", font=_font(25, True), fill="#8B817A")
    img.save(path, "JPEG", quality=90, optimize=True)
    return f"/static/generated/{slug}.jpg"


def generate_site_og() -> str:
    """Create a stable, branded social card without a runtime network dependency."""
    path = OG_DIR / "toolbako-social.jpg"
    if path.exists():
        return "/static/generated/toolbako-social.jpg"
    img = Image.new("RGB", (1200, 630), "#F8F7FF")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((44, 42, 1156, 588), radius=40, fill="#FFFFFF", outline="#DCDDF2", width=3)
    d.rounded_rectangle((76, 75, 320, 555), radius=34, fill="#5557D9")
    d.ellipse((132, 170, 264, 302), fill="#FFFFFF")
    d.rounded_rectangle((166, 138, 230, 345), radius=26, fill="#FFFFFF")
    d.text((370, 105), "TOOLBAKO / AI TOOL MARKET", font=_font(25, True), fill="#5557D9")
    d.text((370, 180), "ツールバコ", font=_fit(d, "ツールバコ", 730, 78, True), fill="#222431")
    d.multiline_text((370, 310), "つくれる人と、\n使いたい人をつなぐ。", font=_font(39, True), fill="#4D4F60", spacing=12)
    d.text((370, 500), "個人のAIツールを、発見・相談・購入", font=_font(24), fill="#77798A")
    img.save(path, "JPEG", quality=90, optimize=True)
    return "/static/generated/toolbako-social.jpg"
