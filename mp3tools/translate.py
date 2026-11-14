"""百度翻译开放平台（通用翻译 API）客户端、签名与字符检测。"""

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


BAIDU_ERROR_HINTS = {
    "52001": "请求超时，请重试",
    "52002": "百度系统错误，请重试",
    "52003": "未授权用户，APP ID 无效或翻译服务未开通",
    "54000": "必填参数为空",
    "54001": "签名错误，请检查密钥是否正确",
    "54003": "访问频率受限，请稍后再试",
    "54004": "账户余额不足",
    "54005": "长文本请求过于频繁",
    "58000": "客户端 IP 非法，请检查百度后台的 IP 限制",
    "58001": "译文语言方向不支持",
    "58002": "服务当前已关闭，请在百度后台启用",
    "90107": "认证未通过或未生效，请检查应用是否已生效",
}

BAIDU_FATAL_CODES = {"52003", "54001", "54004", "58000", "58001", "58002", "90107"}


class TranslateError(ToolError):
    """翻译失败，fatal 表示换 key 之前重试无意义。"""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def baidu_sign(app_id: str, query: str, salt: str, app_secret: str) -> str:
    """百度通用翻译签名：md5(appid + q + salt + 密钥)。"""
    raw = app_id + query + salt + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class Translator:
    """百度翻译开放平台客户端，带磁盘缓存。"""

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
            raise TranslateError("未配置百度翻译 APP ID / 密钥，请先在设置里填写。", fatal=True)
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
        params = {
            "q": text,
            "from": "auto",
            "to": self.settings.target_lang,
            "appid": self.settings.app_key,
            "salt": salt,
            "sign": baidu_sign(
                self.settings.app_key, text, salt, self.settings.app_secret
            ),
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
        except Exception as exc:  # 超时、DNS、证书、连接失败等
            raise TranslateError(f"网络错误：{exc}") from exc

        try:
            result = json.loads(payload)
        except Exception:
            raise TranslateError("接口返回内容不是合法 JSON") from None
        if not isinstance(result, dict):
            raise TranslateError("接口返回格式不正确")

        # 成功时百度返回 52000（部分情况下不带 error_code）
        code = str(result.get("error_code") or "0")
        if code not in ("0", "52000"):
            hint = BAIDU_ERROR_HINTS.get(code) or str(result.get("error_msg") or "未知错误")
            raise TranslateError(f"百度错误 {code}：{hint}", fatal=code in BAIDU_FATAL_CODES)

        trans_result = result.get("trans_result") or []
        if not trans_result:
            raise TranslateError("接口没有返回译文")
        first = trans_result[0] if isinstance(trans_result, list) else {}
        dst = str(first.get("dst", "")).strip() if isinstance(first, dict) else ""
        if not dst:
            raise TranslateError("接口没有返回译文")
        return dst

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
        raise TranslateError("已启用翻译，但缺少百度翻译 APP ID / 密钥，请打开「设置」填写。", fatal=True)
    return Translator(settings, cache_file)
