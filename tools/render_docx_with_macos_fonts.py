"""Render DOCX with macOS Japanese fonts exposed to the bundled LibreOffice."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import sys


RENDERER = Path(
    "/Users/matsumotokengo/.codex/plugins/cache/openai-primary-runtime/"
    "documents/26.709.11516/skills/documents/render_docx.py"
)


def load_renderer():
    spec = importlib.util.spec_from_file_location("codex_render_docx", RENDERER)
    if spec is None or spec.loader is None:
        raise RuntimeError("render_docx.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    renderer = load_renderer()
    original = renderer._build_lo_env

    def build_env(user_profile: str) -> dict:
        env = original(user_profile)
        profile = Path(user_profile)
        font_dirs = [
            profile / "Library" / "Fonts",
            profile / ".fonts",
            profile / "xdg_data" / "fonts",
        ]
        source_fonts = list(Path("/System/Library/Fonts").glob("*角コ*W3.ttc"))
        source_fonts += [Path("/System/Library/Fonts/Hiragino Sans GB.ttc")]
        for font_dir in font_dirs:
            font_dir.mkdir(parents=True, exist_ok=True)
            for source in source_fonts:
                if source.exists():
                    shutil.copyfile(source, font_dir / source.name)
        env["XDG_DATA_HOME"] = str(profile / "xdg_data")
        env["SAL_FONTPATH"] = os.pathsep.join(str(p) for p in font_dirs)
        return env

    renderer._build_lo_env = build_env
    renderer.main()


if __name__ == "__main__":
    main()
