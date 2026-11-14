"""设置模型与持久化（settings.json）。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "MP3 批量处理工具"
APP_VERSION = "1.3"
APP_DIR = Path(__file__).resolve().parent.parent

SETTINGS_FILENAME = "settings.json"
SETTINGS_ENV = "MP3TOOL_SETTINGS"
CACHE_FILENAME = "translate_cache.json"

# 百度翻译开放平台「通用翻译 API」
TRANSLATE_ENDPOINT = "https://fanyi-api.baidu.com/api/trans/vip/translate"
TRANSLATE_APPLY_URL = "https://fanyi-api.baidu.com/"
TARGET_CHOICES = {"zh": "简体中文", "en": "English"}
FIELDS_CHOICES = {"title": "仅歌曲名", "artist": "仅歌手", "both": "歌曲名和歌手"}
FAIL_CHOICES = {"keep": "保留原文继续", "skip": "跳过该文件"}
NAME_FORMATS = {
    "title-artist": "歌曲名 - 歌手",
    "artist-title": "歌手 - 歌曲名",
    "title": "仅歌曲名",
}
ILLEGAL_CHOICES = {"space": "替换为空格", "underscore": "替换为下划线", "remove": "直接删除"}


@dataclass
class Settings:
    """用户设置。默认值即「不翻译、歌名 - 歌手、空格替换」。"""

    # 翻译（百度翻译开放平台）
    translate_enabled: bool = False
    app_key: str = ""
    app_secret: str = ""
    endpoint: str = TRANSLATE_ENDPOINT
    target_lang: str = "zh"
    translate_fields: str = "title"
    accent_as_english: bool = True
    on_translate_fail: str = "keep"
    translation_cache: bool = True
    request_timeout: float = 8.0
    max_retries: int = 1

    # 命名
    name_format: str = "title-artist"
    illegal_char: str = "space"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        result = cls()
        if not isinstance(data, dict):
            return result
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            default = getattr(result, f.name)
            try:
                if isinstance(default, bool):
                    value = bool(value)
                elif isinstance(default, float):
                    value = float(value)
                elif isinstance(default, int):
                    value = int(value)
                elif isinstance(default, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(result, f.name, value)
        # 兼容旧版（有道）设置文件：语言代码与接口地址迁移到百度
        legacy_langs = {"zh-CHS": "zh", "zh-CHT": "zh"}
        result.target_lang = legacy_langs.get(result.target_lang, result.target_lang)
        if "youdao.com" in result.endpoint:
            result.endpoint = TRANSLATE_ENDPOINT
        # 兜底非法值
        if result.target_lang not in TARGET_CHOICES:
            result.target_lang = "zh"
        if result.translate_fields not in FIELDS_CHOICES:
            result.translate_fields = "title"
        if result.on_translate_fail not in FAIL_CHOICES:
            result.on_translate_fail = "keep"
        if result.name_format not in NAME_FORMATS:
            result.name_format = "title-artist"
        if result.illegal_char not in ILLEGAL_CHOICES:
            result.illegal_char = "space"
        result.request_timeout = min(60.0, max(2.0, result.request_timeout))
        result.max_retries = min(5, max(0, result.max_retries))
        return result


def default_settings_path() -> Path:
    """设置文件位置：环境变量 > 脚本旁 settings.json > %APPDATA%。"""
    env = os.environ.get(SETTINGS_ENV)
    if env:
        return Path(env).expanduser()
    local = APP_DIR / SETTINGS_FILENAME
    if local.exists() or os.access(local.parent, os.W_OK):
        return local
    base = os.environ.get("APPDATA")
    root = Path(base) if base else Path.home() / ".config"
    return root / "MP3BatchTools" / SETTINGS_FILENAME


def load_settings(path: Path | str | None = None) -> Settings:
    target = Path(path).expanduser() if path else default_settings_path()
    try:
        if target.is_file():
            # utf-8-sig：兼容记事本 / PowerShell 保存时带的 BOM
            return Settings.from_dict(json.loads(target.read_text(encoding="utf-8-sig")))
    except Exception:
        pass
    return Settings()


def save_settings(settings: Settings, path: Path | str | None = None) -> Path:
    target = Path(path).expanduser() if path else default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def cache_path_for(settings_path: Path | str | None = None) -> Path:
    base = Path(settings_path).expanduser() if settings_path else default_settings_path()
    return base.with_name(CACHE_FILENAME)
