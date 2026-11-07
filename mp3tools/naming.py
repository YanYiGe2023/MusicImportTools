"""文件名生成：清理非法字符、按设置拼接、按需翻译。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from .config import Settings
from .media import read_tags
from .translate import TranslateError, Translator, needs_translation

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str, max_length: int = 120, replacement: str = " ") -> str:
    """去掉 Windows 文件名非法字符，并处理长度和保留名。"""
    name = _INVALID_FILENAME_CHARS.sub(replacement, name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .")
    if name.upper() in _RESERVED_WINDOWS_NAMES:
        name = "_" + name
    return name


def format_name(title: str, artist: str, settings: Settings) -> str | None:
    """按设置的文件名格式生成名字（不含扩展名）；没有标题时返回 None。"""
    title = (title or "").strip()
    artist = (artist or "").strip()
    if not title:
        return None
    if settings.name_format == "artist-title" and artist:
        stem = f"{artist} - {title}"
    elif settings.name_format == "title" or not artist:
        stem = title
    else:
        stem = f"{title} - {artist}"
    replacement = {"space": " ", "underscore": "_", "remove": ""}.get(
        settings.illegal_char, " "
    )
    return sanitize_filename(stem, replacement=replacement) or None


def compute_name(
    ffprobe: str | None,
    src: Path,
    settings: Settings,
    translator: Translator | None = None,
    on_note: Callable[[str, str, str, str], None] | None = None,
) -> str | None:
    """读取标签、按需翻译，生成新文件名（不含扩展名）。

    on_note(kind, field, source, result)：kind 为 "ok"（翻译成功）或 "fail"。
    """
    tags = read_tags(ffprobe, src)
    title = (tags.get("title") or "").strip()
    artist = (tags.get("artist") or "").strip()

    if translator is not None and settings.translate_enabled:
        for field_name, value in (("title", title), ("artist", artist)):
            if not value:
                continue
            if settings.translate_fields not in ("both", field_name):
                continue
            if not needs_translation(value, settings.accent_as_english):
                continue
            try:
                translated = translator.translate(value)
            except TranslateError as exc:
                if settings.on_translate_fail == "skip":
                    raise
                if on_note:
                    on_note("fail", field_name, value, str(exc))
                continue
            if translated and translated != value:
                if on_note:
                    on_note("ok", field_name, value, translated)
                if field_name == "title":
                    title = translated
                else:
                    artist = translated

    return format_name(title, artist, settings)
