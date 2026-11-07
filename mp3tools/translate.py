"""有道智云 v3 翻译客户端、签名与字符检测。"""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .config import Settings
from .errors import ToolError


def _is_chinese_char(ch: str) -> bool:
    name = unicodedata.name(ch, "")
    return "CJK UNIFIED IDEOGRAPH" in name or "CJK COMPATIBILITY IDEOGRAPH" in name


def needs_translation(text: str, accent_as_english: bool = True) -> bool:
    """判断文本里是否有「中/英以外」的文字。

    中日韩汉字视作中文；ASCII 与其他拉丁字母（可配置）视作英文；
    其他文字系统（假名、谚文、西里尔、希腊、泰文等）返回 True。
    数字、标点、emoji 等符号忽略。
    """
    if not text:
        return False
    normalized = unicodedata.normalize("NFKC", text)
    for ch in normalized:
        if ch.isascii():
            continue
        if _is_chinese_char(ch):
            continue
        category = unicodedata.category(ch)
        if not category.startswith("L"):  # 数字、标点、符号、emoji
            continue
        name = unicodedata.name(ch, "")
        if accent_as_english and name.startswith("LATIN"):
            continue
        return True
    return False


YOUDO_ERROR_HINTS = {
    "101": "缺少必填参数",
    "102": "不支持的语言类型",
    "103": "翻译文本过长",
    "108": "应用 ID 无效",
    "110": "无相关服务的有效实例",
    "111": "开发者账号无效",
    "202": "签名校验失败，请检查应用密钥",
    "206": "时间戳无效",
    "401": "账户已欠费",
    "411": "访问频率受限",
    "412": "请求频率超限",
}

YOUDO_FATAL_CODES = {"102", "108", "110", "111", "202", "401"}
YOUDO_APPLY_URL = "https://ai.youdao.com/"


class TranslateError(ToolError):
    """翻译失败，fatal 表示换 key 之前重试无意义。"""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def youdao_truncate(query: str) -> str:
    return query if len(query) <= 20 else query[:10] + str(len(query)) + query[-10:]


def youdao_sign(app_key: str, query: str, salt: str, curtime: str, app_secret: str) -> str:
    raw = app_key + youdao_truncate(query) + salt + curtime + app_secret
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Translator:
    """有道智云 v3 翻译客户端，带磁盘缓存。"""

    def __init__(self, settings: Settings, cache_file: Path | None = None) -> None:
        self.settings = settings
        self.cache_file = cache_file
        self._cache: dict[str, str] = {}
        self.dirty = False
        if settings.translation_cache and cache_file and cache_file.is_file():
            try:
                loaded = json.loads(cache_file.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    self._cache = {str(k): str(v) for k, v in loaded.items()}
            except Exception:
                self._cache = {}

    def _key(self, text: str) -> str:
        return f"{self.settings.target_lang}\n{text}"

    def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return text
        key = self._key(text)
        if self.settings.translation_cache and key in self._cache:
            return self._cache[key]
        result = self._request(text)
        if self.settings.translation_cache:
            self._cache[key] = result
            self.dirty = True
        return result

    def _request(self, text: str) -> str:
        if not self.settings.app_key or not self.settings.app_secret:
            raise TranslateError("未配置有道应用 ID / 应用密钥，请先在设置里填写。", fatal=True)
        last_error: TranslateError | None = None
        for attempt in range(max(1, self.settings.max_retries + 1)):
            try:
                return self._http_translate(text)
            except TranslateError as exc:
                last_error = exc
                if exc.fatal:
                    break
            if attempt < self.settings.max_retries:
                time.sleep(0.4 * (attempt + 1))
        raise last_error or TranslateError("翻译失败")

    def _http_translate(self, text: str) -> str:
        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))
        params = {
            "q": text,
            "from": "auto",
            "to": self.settings.target_lang,
            "appKey": self.settings.app_key,
            "salt": salt,
            "sign": youdao_sign(
                self.settings.app_key, text, salt, curtime, self.settings.app_secret
            ),
            "signType": "v3",
            "curtime": curtime,
        }
        data = urllib.parse.urlencode(params).encode("utf-8")
        request = urllib.request.Request(
            self.settings.endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.request_timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise TranslateError(f"接口返回 HTTP {exc.code}") from exc
        except Exception as exc:  # 超时、DNS、连接失败等
            raise TranslateError(f"网络错误：{exc}") from exc

        try:
            result = json.loads(payload)
        except Exception:
            raise TranslateError("接口返回内容不是合法 JSON") from None
        if not isinstance(result, dict):
            raise TranslateError("接口返回格式不正确")

        code = str(result.get("errorCode", ""))
        if code != "0":
            hint = YOUDO_ERROR_HINTS.get(code, "未知错误")
            raise TranslateError(f"有道错误 {code}：{hint}", fatal=code in YOUDO_FATAL_CODES)

        translations = result.get("translation") or []
        if not translations:
            raise TranslateError("接口没有返回译文")
        return str(translations[0]).strip()

    def save_cache(self) -> None:
        if not (self.settings.translation_cache and self.cache_file and self.dirty):
            return
        try:
            if len(self._cache) > 5000:
                self._cache = dict(list(self._cache.items())[-5000:])
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self._cache, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass


def build_translator(settings: Settings, cache_file: Path | None = None) -> Translator | None:
    if not settings.translate_enabled:
        return None
    if not settings.app_key or not settings.app_secret:
        raise TranslateError("已启用翻译，但缺少有道应用 ID / 应用密钥，请打开「设置」填写。", fatal=True)
    return Translator(settings, cache_file)
