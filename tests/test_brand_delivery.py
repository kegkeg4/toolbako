from pathlib import Path
from PIL import Image


def test_lossless_brand_asset_preserves_all_pixels_and_reduces_bytes():
    root = Path(__file__).resolve().parents[1]
    source = root / 'static/brand/toolbako-logo-header.png'
    optimized = source.with_suffix('.webp')
    with Image.open(source) as before, Image.open(optimized) as after:
        assert before.size == after.size
        assert before.convert('RGBA').tobytes() == after.convert('RGBA').tobytes()
    assert optimized.stat().st_size < source.stat().st_size * .7
