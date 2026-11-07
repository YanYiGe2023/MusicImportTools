"""命令行入口与终端输出。"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Callable

from .batch import JobConfig, collect_files, default_out_dir, output_excludes, run_batch
from .config import APP_NAME, APP_VERSION, TARGET_CHOICES, cache_path_for, load_settings
from .errors import ToolError
from .media import IS_WIN, db_to_percent, find_binary, percent_to_db
from .naming import compute_name
from .translate import TranslateError, build_translator


def _launch_gui() -> int:
    from .gui import launch_gui

    return launch_gui()


def _enable_utf8_console() -> None:
    if not IS_WIN:
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="volume_tool.py",
        description="批量降低 MP3 音量，或按 ID3 标签把文件重命名为「歌曲名 - 歌手」。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python volume_tool.py "D:\\Music" -v 6\n'
            '  python volume_tool.py song.mp3 --percent 50\n'
            '  python volume_tool.py "D:\\Music" --rename\n'
            '  python volume_tool.py "D:\\Music" -v 3 --rename --overwrite\n'
            "  python volume_tool.py            # 打开图形界面\n"
        ),
    )
    p.add_argument("input", nargs="?", help="MP3 文件或文件夹")
    amount = p.add_mutually_exclusive_group()
    amount.add_argument("-v", "--db", type=float, metavar="N", help="降低 N 分贝（常用 3~12）")
    amount.add_argument("-p", "--percent", type=float, metavar="N",
                        help="把音量降到原来的 N%%（例如 50 表示降一半）")
    p.add_argument("--rename", action="store_true",
                   help="按 ID3 标签重命名：歌曲名 - 歌手（单独使用＝只改文件名）")
    p.add_argument("--settings", metavar="FILE", help="设置文件路径（默认脚本旁的 settings.json）")
    p.add_argument("--translate", action="store_true", help="本次启用有道翻译（覆盖设置里的开关）")
    p.add_argument("-o", "--out", metavar="DIR", help="输出文件夹（默认：输入位置下的 output）")
    p.add_argument("--in-place", action="store_true", help="原地覆盖原文件（谨慎使用）")
    p.add_argument("--no-recursive", action="store_true", help="只处理文件夹第一层，不含子文件夹")
    p.add_argument("-j", "--jobs", type=int, default=min(4, os.cpu_count() or 1), metavar="N",
                   help="同时处理的文件数（默认 4）")
    p.add_argument("--bitrate", default="auto", metavar="BPS",
                   help="编码方式：auto（默认 VBR，约 190 kbps）或固定码率如 320k")
    p.add_argument("--overwrite", action="store_true", help="输出文件已存在时覆盖")
    p.add_argument("--dry-run", action="store_true", help="只列出将要处理的文件，不实际转换")
    p.add_argument("--gui", action="store_true", help="打开图形界面")
    p.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    return p


def _cli_emit() -> Callable[..., None]:
    lock = threading.Lock()
    state = {"rename_only": False}

    def emit(event: str, **kw) -> None:
        with lock:
            if event == "start":
                state["rename_only"] = kw["rename_only"]
                n = kw["total"]
                print("=" * 60)
                print(f"{APP_NAME} v{APP_VERSION}")
                print(f"输入    : {kw['input']}")
                if kw["rename_only"]:
                    print("模式    : 只重命名（复制到输出目录，原文件不改动）")
                else:
                    if kw["db"] is not None:
                        amount = f"-{kw['db']:g} dB（约原来的 {db_to_percent(kw['db']):.1f}%）"
                    else:
                        percent = kw["percent"]
                        amount = f"降到原来的 {percent:g}%（约 -{percent_to_db(percent):.2f} dB）"
                    print(f"输出    : {kw['out_dir']}")
                    print(f"音量    : {amount}")
                    if kw["rename"]:
                        print("重命名  : 开启")
                        if kw.get("translate"):
                            target = TARGET_CHOICES.get(kw.get("target_lang", ""), kw.get("target_lang", ""))
                            print(f"翻译    : 开启（有道 → {target}）")
                print(f"文件数  : {n}")
                print("=" * 60)
            elif event == "file_begin":
                print(f"  [{kw['index']:>3}/{kw['total']}] 处理中 {Path(kw['path']).name}")
            elif event == "file_ok":
                old = Path(kw["path"]).name
                new = Path(kw.get("dst") or kw["path"]).name
                if new != old:
                    print(f"  [{kw['index']:>3}/{kw['total']}] 完成   {old} → {new}")
                else:
                    print(f"  [{kw['index']:>3}/{kw['total']}] 完成   {old}")
            elif event == "translated":
                label = "歌曲名" if kw["field"] == "title" else "歌手"
                print(f"       翻译{label}：{kw['source']} → {kw['text']}")
            elif event == "translate_fail":
                label = "歌曲名" if kw["field"] == "title" else "歌手"
                print(f"       翻译失败（{label}）：{kw['error']}")
            elif event == "file_skip":
                print(f"  [{kw['index']:>3}/{kw['total']}] 跳过   {Path(kw['path']).name}（{kw['reason']}）")
            elif event == "file_fail":
                print(f"  [{kw['index']:>3}/{kw['total']}] 失败   {Path(kw['path']).name}")
                print(f"        {kw['error']}")
            elif event == "done":
                s = kw["summary"]
                print("-" * 60)
                print(f"完成：成功 {s.ok}，跳过 {s.skipped}，失败 {len(s.failed)}，用时 {s.seconds:.1f} 秒")
                if s.failed:
                    print("失败列表：")
                    for path, err in s.failed:
                        print(f"  - {path.name}: {err}")
                if state["rename_only"]:
                    print("已输出到输出目录，原文件未改动。")
                elif s.out_dir:
                    print(f"输出目录：{s.out_dir}")

    return emit


def main(argv: list[str] | None = None) -> int:
    _enable_utf8_console()
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:
        return _launch_gui()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui:
        return _launch_gui()

    if args.input is None:
        parser.print_help()
        return 2
    if args.db is None and args.percent is None and not args.rename:
        parser.error("请用 -v/--db、-p/--percent 指定降低量，或加 --rename 只重命名文件")

    try:
        db = abs(args.db) if args.db is not None else None
        percent = abs(args.percent) if args.percent is not None else None
        if db == 0:
            raise ToolError("降低量不能为 0。")
        rename_only = db is None and percent is None

        target = Path(args.input)
        if args.rename and args.in_place:
            raise ToolError(
                "「原地覆盖」与「按标签重命名」不能同时使用："
                "重命名只作用于输出文件，原文件不会被改动。"
            )
        out_dir = None
        if args.in_place and args.out:
            raise ToolError("--in-place 与 --out 不能同时使用。")
        if args.out:
            out_dir = Path(args.out)
        elif not args.in_place:
            out_dir = default_out_dir(target)

        settings_file = Path(args.settings).expanduser() if args.settings else None
        settings = load_settings(settings_file)
        if args.translate:
            settings.translate_enabled = True
        cache_file = cache_path_for(settings_file)

        cfg = JobConfig(
            target=target,
            db=db,
            percent=percent,
            out_dir=out_dir,
            recursive=not args.no_recursive,
            jobs=args.jobs,
            bitrate=args.bitrate,
            overwrite=args.overwrite,
            rename=args.rename,
        )

        if args.dry_run:
            ffprobe = find_binary("ffprobe")
            resolved = target.resolve()
            excludes = output_excludes(resolved, out_dir.resolve() if out_dir else None)
            files = collect_files(resolved, cfg.recursive, cfg.exts, excludes)
            translator = build_translator(settings, cache_file) if args.rename else None
            print(f"共 {len(files)} 个文件：")
            for f in files:
                if args.rename and ffprobe:
                    try:
                        stem = compute_name(ffprobe, f, settings, translator)
                    except TranslateError as exc:
                        print(f"  {f.name}（翻译失败：{exc}）")
                        continue
                    if stem:
                        print(f"  {f.name} → {stem}.mp3")
                    else:
                        print(f"  {f.name}（无标题标签，按原名复制）")
                else:
                    print(f"  {f}")
            if translator is not None:
                translator.save_cache()
            return 0

        summary = run_batch(cfg, _cli_emit(), settings=settings, cache_file=cache_file)
        return 1 if summary.failed else 0
    except ToolError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
