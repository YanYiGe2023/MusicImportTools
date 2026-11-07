"""设置页：翻译、命名、字符识别与失败策略。"""

from __future__ import annotations

import queue
import threading
from typing import Callable

from .config import (
    FAIL_CHOICES,
    FIELDS_CHOICES,
    ILLEGAL_CHOICES,
    NAME_FORMATS,
    TARGET_CHOICES,
    Settings,
    save_settings,
)
from .errors import ToolError
from .translate import Translator

import tkinter as tk
from tkinter import messagebox, ttk


def _choice_key(mapping: dict[str, str], label: str, default: str) -> str:
    for key, value in mapping.items():
        if value == label:
            return key
    return default

class SettingsDialog(tk.Toplevel):
    """设置页：有道翻译、命名格式、字符识别与失败策略。"""

    def __init__(self, master, settings: Settings, on_save: Callable[[Settings], None]) -> None:
        super().__init__(master)
        self.title("设置")
        self.resizable(False, False)
        self.on_save = on_save
        self._result_queue: queue.Queue = queue.Queue()
        self._build(settings)
        self._refresh_translate_state()
        self.transient(master)
        self.grab_set()

    # ---------- 构建界面 ----------
    def _build(self, settings: Settings) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        self.enabled_var = tk.BooleanVar(value=settings.translate_enabled)
        self.key_var = tk.StringVar(value=settings.app_key)
        self.secret_var = tk.StringVar(value=settings.app_secret)
        self.endpoint_var = tk.StringVar(value=settings.endpoint)
        self.target_var = tk.StringVar(value=TARGET_CHOICES.get(settings.target_lang, "简体中文"))
        self.fields_var = tk.StringVar(value=FIELDS_CHOICES.get(settings.translate_fields, "仅歌曲名"))
        self.show_secret = tk.BooleanVar(value=False)
        self.accent_var = tk.BooleanVar(value=settings.accent_as_english)
        self.fail_var = tk.StringVar(value=FAIL_CHOICES.get(settings.on_translate_fail, "保留原文继续"))
        self.cache_var = tk.BooleanVar(value=settings.translation_cache)
        self.timeout_var = tk.StringVar(value=f"{settings.request_timeout:g}")
        self.retry_var = tk.StringVar(value=str(settings.max_retries))
        self.format_var = tk.StringVar(value=NAME_FORMATS.get(settings.name_format, "歌曲名 - 歌手"))
        self.illegal_var = tk.StringVar(value=ILLEGAL_CHOICES.get(settings.illegal_char, "替换为空格"))
        self.status_var = tk.StringVar(value="填写后点「测试翻译」验证密钥是否可用")

        self._build_translate(outer, settings)
        self._build_naming(outer)
        self._build_behavior(outer)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="恢复默认", command=self._restore).pack(side="left")
        ttk.Button(buttons, text="取消", command=self.destroy, width=8).pack(side="right")
        ttk.Button(buttons, text="保存", command=self._save, width=8).pack(side="right", padx=(0, 8))

    def _build_translate(self, parent: ttk.Frame, settings: Settings) -> None:
        box = ttk.LabelFrame(parent, text=" 翻译（有道智云 v3） ", padding=10)
        box.pack(fill="x")

        ttk.Checkbutton(
            box,
            text="启用翻译：歌曲名 / 歌手出现中、英以外字符时调用有道翻译",
            variable=self.enabled_var,
            command=self._refresh_translate_state,
        ).pack(anchor="w")

        grid = ttk.Frame(box)
        grid.pack(fill="x", pady=(8, 0))
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="应用 ID").grid(row=0, column=0, sticky="w")
        self.key_entry = ttk.Entry(grid, textvariable=self.key_var)
        self.key_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))

        ttk.Label(grid, text="应用密钥").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.secret_entry = ttk.Entry(grid, textvariable=self.secret_var, show="●")
        self.secret_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(6, 0))
        ttk.Checkbutton(
            grid, text="显示", variable=self.show_secret, command=self._toggle_secret,
        ).grid(row=1, column=3, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(grid, text="接口地址").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.endpoint_entry = ttk.Entry(grid, textvariable=self.endpoint_var)
        self.endpoint_entry.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="目标语言").pack(side="left")
        self.target_combo = ttk.Combobox(
            row, textvariable=self.target_var, width=12, state="readonly",
            values=tuple(TARGET_CHOICES.values()),
        )
        self.target_combo.pack(side="left", padx=(8, 18))
        ttk.Label(row, text="翻译内容").pack(side="left")
        self.fields_combo = ttk.Combobox(
            row, textvariable=self.fields_var, width=14, state="readonly",
            values=tuple(FIELDS_CHOICES.values()),
        )
        self.fields_combo.pack(side="left", padx=(8, 0))

        test_row = ttk.Frame(box)
        test_row.pack(fill="x", pady=(10, 0))
        self.test_btn = ttk.Button(test_row, text="测试翻译", command=self._test_connection, width=10)
        self.test_btn.pack(side="left")
        ttk.Label(
            test_row, textvariable=self.status_var, foreground="#666666", wraplength=440,
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            box,
            text="申请地址：https://ai.youdao.com/ （需开通「文本翻译」服务）。"
                 "密钥以明文保存在 settings.json，请勿分享该文件。",
            foreground="#666666", wraplength=620,
        ).pack(anchor="w", pady=(8, 0))

    def _build_naming(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" 命名 ", padding=10)
        box.pack(fill="x", pady=(12, 0))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="文件名格式").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.format_var, width=16, state="readonly",
            values=tuple(NAME_FORMATS.values()),
        ).pack(side="left", padx=(8, 22))
        ttk.Label(row, text="非法字符").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.illegal_var, width=14, state="readonly",
            values=tuple(ILLEGAL_CHOICES.values()),
        ).pack(side="left", padx=(8, 0))

        ttk.Label(
            box, text="只改输出文件名，原文件始终不动。",
            foreground="#666666",
        ).pack(anchor="w", pady=(6, 0))

    def _build_behavior(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text=" 字符识别与容错 ", padding=10)
        box.pack(fill="x", pady=(12, 0))

        ttk.Checkbutton(
            box, text="把带重音符号的拉丁字母视为英文（如 Café、Björk）",
            variable=self.accent_var,
        ).pack(anchor="w")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="翻译失败时").pack(side="left")
        ttk.Combobox(
            row, textvariable=self.fail_var, width=16, state="readonly",
            values=tuple(FAIL_CHOICES.values()),
        ).pack(side="left", padx=(8, 22))
        ttk.Checkbutton(row, text="缓存翻译结果", variable=self.cache_var).pack(side="left")

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="超时（秒）").pack(side="left")
        ttk.Spinbox(row2, from_=2, to=60, width=5, textvariable=self.timeout_var).pack(
            side="left", padx=(8, 18)
        )
        ttk.Label(row2, text="失败重试").pack(side="left")
        ttk.Spinbox(row2, from_=0, to=5, width=5, textvariable=self.retry_var).pack(
            side="left", padx=(8, 0)
        )

    # ---------- 交互 ----------
    def _toggle_secret(self) -> None:
        self.secret_entry.configure(show="" if self.show_secret.get() else "●")

    def _refresh_translate_state(self) -> None:
        state = "normal" if self.enabled_var.get() else "disabled"
        for widget in (self.key_entry, self.secret_entry, self.endpoint_entry):
            widget.configure(state=state)
        for widget in (self.target_combo, self.fields_combo):
            widget.configure(state="readonly" if state == "normal" else "disabled")
        self.test_btn.configure(state=state)

    def _restore(self) -> None:
        defaults = Settings()
        self.enabled_var.set(defaults.translate_enabled)
        self.key_var.set(defaults.app_key)
        self.secret_var.set(defaults.app_secret)
        self.endpoint_var.set(defaults.endpoint)
        self.target_var.set(TARGET_CHOICES[defaults.target_lang])
        self.fields_var.set(FIELDS_CHOICES[defaults.translate_fields])
        self.accent_var.set(defaults.accent_as_english)
        self.fail_var.set(FAIL_CHOICES[defaults.on_translate_fail])
        self.cache_var.set(defaults.translation_cache)
        self.timeout_var.set(f"{defaults.request_timeout:g}")
        self.retry_var.set(str(defaults.max_retries))
        self.format_var.set(NAME_FORMATS[defaults.name_format])
        self.illegal_var.set(ILLEGAL_CHOICES[defaults.illegal_char])
        self._refresh_translate_state()
        self.status_var.set("已恢复默认值（尚未保存）")

    def _collect(self, strict: bool = True) -> Settings:
        enabled = self.enabled_var.get()
        key = self.key_var.get().strip()
        secret = self.secret_var.get().strip()
        if strict and enabled and not key:
            raise ToolError("启用翻译前，请填写有道的应用 ID。")
        if strict and enabled and not secret:
            raise ToolError("启用翻译前，请填写有道的应用密钥。")
        endpoint = self.endpoint_var.get().strip() or "https://openapi.youdao.com/api"
        if not endpoint.startswith(("http://", "https://")):
            raise ToolError("接口地址必须以 http:// 或 https:// 开头。")
        try:
            timeout = float(self.timeout_var.get())
        except ValueError:
            raise ToolError("超时时间必须是数字。") from None
        try:
            retries = int(self.retry_var.get())
        except ValueError:
            raise ToolError("重试次数必须是整数。") from None
        return Settings(
            translate_enabled=enabled,
            app_key=key,
            app_secret=secret,
            endpoint=endpoint,
            target_lang=_choice_key(TARGET_CHOICES, self.target_var.get(), "zh-CHS"),
            translate_fields=_choice_key(FIELDS_CHOICES, self.fields_var.get(), "title"),
            accent_as_english=self.accent_var.get(),
            on_translate_fail=_choice_key(FAIL_CHOICES, self.fail_var.get(), "keep"),
            translation_cache=self.cache_var.get(),
            request_timeout=min(60.0, max(2.0, timeout)),
            max_retries=min(5, max(0, retries)),
            name_format=_choice_key(NAME_FORMATS, self.format_var.get(), "title-artist"),
            illegal_char=_choice_key(ILLEGAL_CHOICES, self.illegal_var.get(), "space"),
        )

    def _save(self) -> None:
        try:
            settings = self._collect(strict=True)
        except ToolError as exc:
            messagebox.showwarning("提示", str(exc), parent=self)
            return
        try:
            save_settings(settings)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self.on_save(settings)
        self.destroy()

    def _test_connection(self) -> None:
        try:
            settings = self._collect(strict=False)
        except ToolError as exc:
            self.status_var.set(str(exc))
            return
        if not settings.app_key or not settings.app_secret:
            self.status_var.set("请先填写应用 ID 和应用密钥")
            return
        settings.translate_enabled = True
        sample = "こんにちは" if settings.target_lang == "zh-CHS" else "你好"
        self.test_btn.configure(state="disabled")
        self.status_var.set("正在测试…")
        threading.Thread(
            target=self._test_worker, args=(settings, sample), daemon=True
        ).start()
        self.after(120, lambda: self._poll_test(0))

    def _test_worker(self, settings: Settings, sample: str) -> None:
        try:
            translator = Translator(settings, None)
            self._result_queue.put(("ok", translator.translate(sample)))
        except Exception as exc:  # noqa: BLE001
            self._result_queue.put(("fail", str(exc)))

    def _poll_test(self, attempts: int) -> None:
        try:
            kind, message = self._result_queue.get_nowait()
        except queue.Empty:
            if attempts < 600:
                self.after(120, lambda: self._poll_test(attempts + 1))
            else:
                self.test_btn.configure(state="normal")
                self.status_var.set("测试超时")
            return
        self.test_btn.configure(state="normal")
        if kind == "ok":
            self.status_var.set(f"连接正常，示例译文：{message}")
        else:
            self.status_var.set(f"测试失败：{message}")
