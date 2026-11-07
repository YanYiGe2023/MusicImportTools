"""图形界面：主窗口。设置页在 settings_ui 中。"""

from __future__ import annotations

import os
import queue
import threading
from pathlib import Path

from .batch import JobConfig, default_out_dir, run_batch
from .config import (
    APP_NAME,
    APP_VERSION,
    TARGET_CHOICES,
    Settings,
    cache_path_for,
    default_settings_path,
    load_settings,
)
from .errors import ToolError
from .media import db_to_percent, percent_to_db

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _TK_AVAILABLE = True
except Exception:  # noqa: BLE001 - 无图形环境时退回命令行
    tk = None  # type: ignore[assignment]
    _TK_AVAILABLE = False


if _TK_AVAILABLE:
    from .settings_ui import SettingsDialog


class VolumeToolApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("820x700")
        self.minsize(740, 620)
        self.q: queue.Queue = queue.Queue()
        self.running = False
        self.total = 0
        self.completed = 0
        self.rename_only_run = False
        self.last_out: str | None = None
        self.settings_file = default_settings_path()
        self.settings = load_settings(self.settings_file)
        self.translate_cache = cache_path_for(self.settings_file)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain)

    # ---------- 界面 ----------
    def _build_ui(self) -> None:
        try:
            ttk.Style(self).theme_use("vista")
        except Exception:
            pass
        self.option_add("*Font", ("Microsoft YaHei UI", 10))

        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # 输入
        box_in = ttk.LabelFrame(root, text=" 输入 ", padding=10)
        box_in.grid(row=0, column=0, sticky="ew")
        box_in.columnconfigure(0, weight=1)
        self.input_var = tk.StringVar()
        ttk.Entry(box_in, textvariable=self.input_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(box_in, text="选择文件…", command=self._pick_file, width=11).grid(row=0, column=1, padx=2)
        ttk.Button(box_in, text="选择文件夹…", command=self._pick_folder, width=12).grid(row=0, column=2, padx=(2, 0))
        ttk.Label(
            box_in, text="支持单个 MP3 文件，或整个文件夹（默认包含子文件夹）。",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 操作
        box_task = ttk.LabelFrame(root, text=" 操作 ", padding=10)
        box_task.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self.task_var = tk.StringVar(value="volume")
        ttk.Radiobutton(
            box_task, text="降低音量", value="volume", variable=self.task_var,
            command=self._refresh_states,
        ).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            box_task, text="只重命名（不降音量）", value="rename", variable=self.task_var,
            command=self._refresh_states,
        ).grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.rename_var = tk.BooleanVar(value=False)
        self.rename_chk = ttk.Checkbutton(
            box_task, text="按 ID3 标签重命名：歌曲名 - 歌手", variable=self.rename_var,
        )
        self.rename_chk.grid(row=0, column=2, sticky="w", padx=(22, 0))
        ttk.Label(
            box_task,
            text="重命名只作用于输出文件，原文件始终不改动。",
            foreground="#666666",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 降低量
        box_amt = ttk.LabelFrame(root, text=" 降低量 ", padding=10)
        box_amt.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(box_amt, text="数值").grid(row=0, column=0, sticky="w")
        self.value_var = tk.StringVar(value="6")
        self.value_entry = ttk.Entry(box_amt, textvariable=self.value_var, width=8)
        self.value_entry.grid(row=0, column=1, padx=(8, 8))
        self.unit_var = tk.StringVar(value="分贝 dB")
        self.unit_combo = ttk.Combobox(
            box_amt, textvariable=self.unit_var, width=12, state="readonly",
            values=("分贝 dB", "百分比 %"),
        )
        self.unit_combo.grid(row=0, column=2)
        self.hint_var = tk.StringVar()
        ttk.Label(box_amt, textvariable=self.hint_var, foreground="#666666").grid(
            row=0, column=3, sticky="w", padx=(14, 0)
        )
        self.value_var.trace_add("write", lambda *_: self._update_hint())
        self.unit_var.trace_add("write", lambda *_: self._update_hint())
        self._update_hint()

        # 输出
        box_out = ttk.LabelFrame(root, text=" 输出 ", padding=10)
        box_out.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        box_out.columnconfigure(1, weight=1)
        self.mode_var = tk.StringVar(value="folder")
        self.radio_out = ttk.Radiobutton(
            box_out, text="输出到文件夹", value="folder", variable=self.mode_var,
            command=self._refresh_states,
        )
        self.radio_out.grid(row=0, column=0, sticky="w")
        self.radio_inplace = ttk.Radiobutton(
            box_out, text="覆盖原文件", value="inplace", variable=self.mode_var,
            command=self._refresh_states,
        )
        self.radio_inplace.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.out_var = tk.StringVar()
        self.out_entry = ttk.Entry(box_out, textvariable=self.out_var)
        self.out_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.out_btn = ttk.Button(box_out, text="浏览…", command=self._pick_out, width=8)
        self.out_btn.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        self.out_hint = ttk.Label(
            box_out, text="留空则自动输出到「输入位置\\output」，原文件不会被改动。",
            foreground="#666666",
        )
        self.out_hint.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # 操作按钮
        box_run = ttk.Frame(root)
        box_run.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        box_run.columnconfigure(0, weight=1)
        self.start_btn = ttk.Button(box_run, text="开始处理", command=self._start)
        self.start_btn.grid(row=0, column=0, sticky="w")
        self.settings_btn = ttk.Button(box_run, text="设置…", command=self._open_settings, width=8)
        self.settings_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.open_btn = ttk.Button(box_run, text="打开输出文件夹", command=self._open_out,
                                   state="disabled", width=16)
        self.open_btn.grid(row=0, column=2, sticky="e")
        self.progress = ttk.Progressbar(box_run, mode="determinate", maximum=100)
        self.progress.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(box_run, textvariable=self.status_var).grid(row=2, column=0, columnspan=3,
                                                             sticky="w", pady=(4, 0))

        # 日志
        box_log = ttk.LabelFrame(root, text=" 日志 ", padding=8)
        box_log.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        root.rowconfigure(5, weight=1)
        box_log.rowconfigure(0, weight=1)
        box_log.columnconfigure(0, weight=1)
        self.log = tk.Text(box_log, height=10, wrap="none", state="disabled",
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat",
                           font=("Microsoft YaHei UI", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box_log, orient="vertical", command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)
        self._refresh_states()

    def _refresh_states(self) -> None:
        """根据「操作」和「输出」两个选择，启用/禁用相关控件。"""
        if self.running:
            return
        volume = self.task_var.get() == "volume"
        if not volume:
            self.mode_var.set("folder")  # 只重命名必须输出到文件夹
        folder = self.mode_var.get() == "folder"

        self.value_entry.configure(state="normal" if volume else "disabled")
        self.unit_combo.configure(state="readonly" if volume else "disabled")
        self.radio_out.configure(state="normal" if volume else "disabled")
        self.radio_inplace.configure(state="normal" if volume else "disabled")
        entry_state = "normal" if folder else "disabled"
        self.out_entry.configure(state=entry_state)
        self.out_btn.configure(state=entry_state)
        if not volume:
            self.rename_var.set(True)
            self.rename_chk.configure(state="disabled")
            self.start_btn.configure(text="开始重命名")
            self.out_hint.configure(text="重命名后的文件输出到这里，原文件不会被改动。")
        else:
            # 覆盖原文件时不能重命名（否则会改到原文件名）
            self.rename_chk.configure(state="normal" if folder else "disabled")
            if not folder:
                self.rename_var.set(False)
            self.start_btn.configure(text="开始处理")
            self.out_hint.configure(
                text="留空则自动输出到「输入位置\\output」，原文件不会被改动。" if folder
                else "注意：处理成功后会直接替换原文件。"
            )

    def _update_hint(self) -> None:
        if self.task_var.get() != "volume":
            self.hint_var.set("")
            return
        raw = self.value_var.get().strip()
        try:
            value = abs(float(raw))
        except ValueError:
            self.hint_var.set("请输入数字")
            return
        if "分贝" in self.unit_var.get():
            if value <= 0:
                self.hint_var.set("请输入大于 0 的数")
            else:
                self.hint_var.set(f"约等于原来的 {db_to_percent(value):.1f}%")
        else:
            if not (0 < value < 100):
                self.hint_var.set("请输入 0~100 之间的数")
            else:
                self.hint_var.set(f"约等于 -{percent_to_db(value):.2f} dB")

    # ---------- 选择 ----------
    def _pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 MP3 文件",
            filetypes=[("MP3 音频", "*.mp3"), ("所有文件", "*.*")],
        )
        if path:
            self.input_var.set(path)

    def _pick_folder(self) -> None:
        path = filedialog.askdirectory(title="选择包含 MP3 的文件夹")
        if path:
            self.input_var.set(path)

    def _pick_out(self) -> None:
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, path)

    def _open_out(self) -> None:
        target = self.last_out
        if target and Path(target).is_dir():
            try:
                os.startfile(target)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("无法打开", str(exc))

    # ---------- 设置 ----------
    def _open_settings(self) -> None:
        SettingsDialog(self, self.settings, self._on_settings_saved)

    def _on_settings_saved(self, settings: Settings) -> None:
        self.settings = settings
        self.settings_file = default_settings_path()
        self.translate_cache = cache_path_for(self.settings_file)
        self._log("设置已保存。")
        if settings.translate_enabled:
            self._log(f"翻译：开启（有道 → {TARGET_CHOICES.get(settings.target_lang, settings.target_lang)}）")

    # ---------- 运行 ----------
    def _build_config(self) -> JobConfig:
        raw = self.input_var.get().strip().strip('"')
        if not raw:
            raise ToolError("请先选择 MP3 文件或文件夹。")
        target = Path(raw)
        if not target.exists():
            raise ToolError(f"路径不存在：{target}")

        if self.task_var.get() == "rename":
            typed = self.out_entry.get().strip().strip('"')
            out_dir = Path(typed) if typed else default_out_dir(target)
            return JobConfig(target=target, db=None, percent=None, out_dir=out_dir,
                             rename=True)

        text = self.value_var.get().strip()
        try:
            value = abs(float(text))
        except ValueError:
            raise ToolError("降低量必须是数字。") from None
        if value == 0:
            raise ToolError("降低量不能为 0。")

        if "分贝" in self.unit_var.get():
            db, percent = value, None
        else:
            if not (0 < value < 100):
                raise ToolError("百分比必须在 0 到 100 之间。")
            db, percent = None, value

        if self.mode_var.get() == "inplace":
            out_dir = None
        else:
            typed = self.out_entry.get().strip().strip('"')
            out_dir = Path(typed) if typed else default_out_dir(target)

        return JobConfig(target=target, db=db, percent=percent, out_dir=out_dir,
                         rename=bool(self.rename_var.get()))

    def _start(self) -> None:
        if self.running:
            return
        try:
            cfg = self._build_config()
        except ToolError as exc:
            messagebox.showwarning("提示", str(exc))
            return

        self.running = True
        self.total = 0
        self.completed = 0
        self.rename_only_run = cfg.db is None and cfg.percent is None
        self.last_out = None
        self.progress.configure(value=0)
        self.status_var.set("正在准备…")
        self._clear_log()
        self.start_btn.configure(state="disabled")
        self.settings_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        threading.Thread(target=self._worker, args=(cfg,), daemon=True).start()

    def _worker(self, cfg: JobConfig) -> None:
        settings = self.settings
        try:
            run_batch(cfg, emit=lambda event, **kw: self.q.put((event, kw)),
                      settings=settings, cache_file=self.translate_cache)
        except Exception as exc:  # noqa: BLE001
            self.q.put(("error", {"message": str(exc)}))
        finally:
            self.q.put(("worker_end", {}))

    def _drain(self) -> None:
        try:
            while True:
                event, kw = self.q.get_nowait()
                self._handle(event, kw)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _handle(self, event: str, kw: dict) -> None:
        if event == "start":
            self.total = kw["total"]
            self.last_out = kw["out_dir"]
            if kw.get("rename_only"):
                self._log(f"输入：{kw['input']}")
                self._log(f"输出：{kw['out_dir']}")
                self._log(f"模式：只重命名（歌曲名 - 歌手）　文件数：{self.total}")
            else:
                amount = (f"-{kw['db']:g} dB" if kw.get("db")
                          else f"降到原来的 {kw['percent']:g}%")
                self._log(f"输入：{kw['input']}")
                self._log(f"输出：{kw['out_dir']}")
                self._log(f"音量：{amount}　文件数：{self.total}")
                if kw.get("rename"):
                    self._log("重命名：开启")
                    if kw.get("translate"):
                        target = TARGET_CHOICES.get(kw.get("target_lang", ""), kw.get("target_lang", ""))
                        self._log(f"翻译：开启（有道 → {target}）")
        elif event == "translated":
            label = "歌曲名" if kw["field"] == "title" else "歌手"
            self._log(f"翻译{label}：{kw['source']} → {kw['text']}")
        elif event == "translate_fail":
            label = "歌曲名" if kw["field"] == "title" else "歌手"
            self._log(f"翻译失败（{label}）：{kw['error']}")
        elif event == "file_begin":
            self.status_var.set(f"正在处理 [{kw['index']}/{self.total}] {Path(kw['path']).name}")
        elif event == "file_progress":
            if self.total:
                overall = (self.completed + kw["fraction"]) / self.total * 100
                self.progress.configure(value=min(100, overall))
        elif event == "file_ok":
            self.completed += 1
            old = Path(kw["path"]).name
            new = Path(kw["dst"]).name
            self._log(f"[完成] {old} → {new}" if new != old else f"[完成] {old}")
            self._update_progress()
        elif event == "file_skip":
            self.completed += 1
            self._log(f"[跳过] {Path(kw['path']).name}（{kw['reason']}）")
            self._update_progress()
        elif event == "file_fail":
            self.completed += 1
            self._log(f"[失败] {Path(kw['path']).name}：{kw['error']}")
            self._update_progress()
        elif event == "done":
            s = kw["summary"]
            self.progress.configure(value=100)
            self._log("-" * 50)
            self._log(f"成功 {s.ok}，跳过 {s.skipped}，失败 {len(s.failed)}，"
                      f"用时 {s.seconds:.1f} 秒")
            self.status_var.set(
                f"完成：成功 {s.ok}，跳过 {s.skipped}，失败 {len(s.failed)}"
            )
            self.open_btn.configure(state="normal" if self.last_out else "disabled")
            if s.failed:
                messagebox.showwarning(
                    "部分文件失败",
                    f"成功 {s.ok} 个，失败 {len(s.failed)} 个。\n"
                    "详情见日志。\n\n" + "\n".join(f"{p.name}: {e}" for p, e in s.failed[:5]),
                )
            elif self.rename_only_run:
                messagebox.showinfo(
                    "完成",
                    f"已输出 {s.ok} 个文件。\n输出：{s.out_dir}\n原文件未改动。",
                )
            else:
                messagebox.showinfo("完成", f"已处理 {s.ok} 个文件。\n输出：{s.out_dir}")
        elif event == "error":
            self.status_var.set("出错")
            self._log(f"[错误] {kw['message']}")
            messagebox.showerror("出错", kw["message"])
        elif event == "worker_end":
            self.running = False
            self.start_btn.configure(state="normal")
            self.settings_btn.configure(state="normal")
            self._refresh_states()

    def _update_progress(self) -> None:
        if self.total:
            self.progress.configure(value=self.completed / self.total * 100)
            self.status_var.set(f"已完成 {self.completed}/{self.total}")

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno("确认退出", "正在处理文件，确定要退出吗？"):
                return
        self.destroy()


def launch_gui() -> int:
    if not _TK_AVAILABLE:
        print("无法启动图形界面（缺少 tkinter），请改用命令行参数。")
        return 1
    VolumeToolApp().mainloop()
    return 0
