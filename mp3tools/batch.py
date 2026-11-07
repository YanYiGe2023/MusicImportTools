"""批量任务：文件收集、命名规划、编码/复制执行。"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings
from .errors import ToolError
from .media import (
    SUBPROCESS_FLAGS,
    audio_codec_args,
    build_filter,
    find_binary,
    probe_audio,
    resolve_tools,
)
from .naming import compute_name
from .translate import TranslateError, build_translator


def collect_files(
    target: Path,
    recursive: bool,
    exts: tuple[str, ...],
    exclude_dirs: tuple[Path, ...] = (),
) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() not in exts:
            raise ToolError(f"不是支持的文件类型：{target.suffix}（支持：{', '.join(exts)}）")
        return [target]
    if not target.is_dir():
        raise ToolError(f"路径不存在：{target}")

    pattern = "*"
    iterator = target.rglob(pattern) if recursive else target.glob(pattern)
    files: list[Path] = []
    for p in iterator:
        try:
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            if any(_is_inside(p, d) for d in exclude_dirs):
                continue  # 跳过输出目录，避免重复处理
            files.append(p)
        except OSError:
            continue
    files.sort(key=lambda p: str(p).lower())
    return files


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def output_excludes(target: Path, out_dir: Path | None) -> tuple[Path, ...]:
    """需要从扫描中排除的目录：默认 output 目录，以及位于输入目录内的输出目录。"""
    if not target.is_dir():
        return ()
    dirs: list[Path] = []
    default = target / "output"
    if default.is_dir():
        dirs.append(default)
    if out_dir is not None and out_dir != target and _is_inside(out_dir, target):
        dirs.append(out_dir)
    return tuple(dict.fromkeys(dirs))


def default_out_dir(target: Path) -> Path:
    base = target if target.is_dir() else target.parent
    return base / "output"


@dataclass
class JobConfig:
    target: Path
    db: float | None = None
    percent: float | None = None
    out_dir: Path | None = None  # None 表示原地覆盖
    recursive: bool = True
    jobs: int = 4
    bitrate: str = "auto"
    overwrite: bool = False
    rename: bool = False
    exts: tuple[str, ...] = (".mp3",)


@dataclass
class Summary:
    total: int = 0
    ok: int = 0
    skipped: int = 0
    failed: list[tuple[Path, str]] = field(default_factory=list)
    seconds: float = 0.0
    out_dir: str = ""


# --------------------------------------------------------------------------
# 单文件编码
# --------------------------------------------------------------------------

class _EncodeError(Exception):
    pass


def _encode(
    ffmpeg: str,
    src: Path,
    tmp: Path,
    filter_expr: str,
    audio_args: list[str],
    duration: float,
    on_fraction: Callable[[float], None] | None,
    with_cover: bool,
) -> None:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    cmd += ["-map", "0:a"]
    if with_cover:
        cmd += ["-map", "0:v?", "-c:v", "copy"]
    cmd += [
        "-map_metadata", "0",
        "-id3v2_version", "3",
        "-af", filter_expr,
        *audio_args,
        "-progress", "pipe:1",
        "-nostats",
        "-loglevel", "error",
        str(tmp),
    ]

    err_lines: list[str] = []
    last_reported = 0.0
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **SUBPROCESS_FLAGS,
    )
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            if line.startswith("out_time_us="):
                if on_fraction and duration > 0:
                    try:
                        micros = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    frac = min(1.0, max(0.0, micros / 1_000_000.0 / duration))
                    if frac - last_reported >= 0.02 or frac >= 0.999:
                        last_reported = frac
                        on_fraction(frac)
            elif "=" not in line:
                err_lines.append(line)
                if len(err_lines) > 40:
                    del err_lines[:20]
    finally:
        proc.stdout.close()
        rc = proc.wait()

    if rc != 0:
        detail = "\n".join(err_lines[-4:]).strip() or f"ffmpeg 返回码 {rc}"
        raise _EncodeError(detail)


@dataclass
class _Ctx:
    ffmpeg: str
    ffprobe: str | None
    filter_expr: str
    audio_args: list[str]
    emit: Callable[..., None]
    total: int
    in_place: bool = False


def _process_one(ctx: _Ctx, index: int, src: Path, dst: Path) -> tuple[str, str]:
    ctx.emit("file_begin", index=index, total=ctx.total, path=str(src))

    duration, _ = probe_audio(ctx.ffprobe, src)

    def on_fraction(frac: float) -> None:
        ctx.emit("file_progress", index=index, fraction=frac)

    tmp = dst.with_name(dst.name + ".part.mp3")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    try:
        try:
            _encode(ctx.ffmpeg, src, tmp, ctx.filter_expr, ctx.audio_args, duration, on_fraction, with_cover=True)
        except _EncodeError:
            # 个别文件封面/附加流异常，去掉封面重试一次
            _encode(ctx.ffmpeg, src, tmp, ctx.filter_expr, ctx.audio_args, duration, on_fraction, with_cover=False)
        os.replace(tmp, dst)
        if ctx.in_place and dst != src:
            # 原地模式改名后，原文件已被新文件取代，删掉旧文件
            try:
                src.unlink()
            except OSError:
                pass
        return "ok", ""
    except Exception as exc:  # noqa: BLE001 - 汇总给用户
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return "fail", str(exc)


# --------------------------------------------------------------------------
# 批处理主流程
# --------------------------------------------------------------------------

def _run_task(ctx: _Ctx, index: int, src: Path, dst: Path, mode: str) -> tuple[str, str]:
    """执行单个任务：复制到输出（只重命名），或降音量编码。"""
    if mode == "encode":
        return _process_one(ctx, index, src, dst)

    ctx.emit("file_begin", index=index, total=ctx.total, path=str(src))
    tmp = dst.with_name(dst.name + ".part")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
        return "ok", ""
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        return "fail", f"复制失败：{exc}"


def run_batch(
    cfg: JobConfig,
    emit: Callable[..., None] | None = None,
    settings: Settings | None = None,
    cache_file: Path | None = None,
) -> Summary:
    emit = emit or (lambda *a, **k: None)
    settings = settings or Settings()
    started = time.monotonic()

    rename_only = cfg.db is None and cfg.percent is None
    if rename_only and not cfg.rename:
        raise ToolError("请指定降低量（-v 或 -p），或使用 --rename 只重命名输出文件。")
    if cfg.db is not None and cfg.db <= 0:
        raise ToolError("分贝数值必须大于 0。")
    if cfg.percent is not None and not (0 < cfg.percent < 100):
        raise ToolError("百分比必须在 0 到 100 之间。")
    if cfg.rename and cfg.out_dir is None:
        raise ToolError(
            "「覆盖原文件」与「按标签重命名」不能同时使用："
            "重命名只作用于输出文件，原文件不会被改动。"
        )

    if rename_only:
        ffmpeg = ""
        ffprobe = find_binary("ffprobe")
        if cfg.rename and not ffprobe:
            raise ToolError("按标签重命名需要 ffprobe，请确认 ffmpeg 安装完整。")
    else:
        ffmpeg, ffprobe = resolve_tools()
        if cfg.rename and not ffprobe:
            raise ToolError("按标签重命名需要 ffprobe，请确认 ffmpeg 安装完整。")

    translator: Translator | None = None
    if cfg.rename:
        translator = build_translator(settings, cache_file)

    target = Path(cfg.target).expanduser()
    if not target.exists():
        raise ToolError(f"路径不存在：{target}")
    target = target.resolve()

    out_dir: Path | None = None
    if cfg.out_dir is not None:
        out_dir = Path(cfg.out_dir).expanduser().resolve()
        input_dir = target if target.is_dir() else target.parent
        if out_dir == input_dir:
            raise ToolError(
                "输出目录不能和原文件所在目录相同。"
                "如需覆盖原文件请用 --in-place，或换一个输出目录。"
            )

    excludes = output_excludes(target, out_dir)
    files = collect_files(target, cfg.recursive, cfg.exts, excludes)
    if not files:
        raise ToolError("没有找到可处理的 MP3 文件。")

    filter_expr = None if rename_only else build_filter(cfg.db, cfg.percent)
    total = len(files)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)

    display_out = str(out_dir) if out_dir else str(target if target.is_dir() else target.parent)

    emit(
        "start",
        total=total,
        input=str(target),
        out_dir=display_out,
        db=cfg.db,
        percent=cfg.percent,
        rename=cfg.rename,
        rename_only=rename_only,
        translate=bool(translator),
        target_lang=settings.target_lang,
        in_place=out_dir is None,
    )

    summary = Summary(total=total, out_dir=display_out)
    ctx = _Ctx(ffmpeg=ffmpeg, ffprobe=ffprobe, filter_expr=filter_expr or "",
               audio_args=audio_codec_args(cfg.bitrate), emit=emit, total=total,
               in_place=out_dir is None)

    tasks: list[tuple[int, Path, Path, str]] = []
    planned: set[Path] = set()

    def skip(index: int, src: Path, dst: Path, reason: str) -> None:
        emit("file_skip", index=index, total=total, path=str(src), dst=str(dst), reason=reason)
        summary.skipped += 1
        emit("file_done", index=index, total=total, status="skip")

    for index, src in enumerate(files, start=1):
        new_name: str | None = None
        if cfg.rename:
            def _note(kind: str, field_name: str, source_text: str, result: str,
                      _index: int = index) -> None:
                if kind == "ok":
                    emit("translated", index=_index, total=total, field=field_name,
                         source=source_text, text=result)
                else:
                    emit("translate_fail", index=_index, total=total, field=field_name,
                         source=source_text, error=result)

            try:
                stem = compute_name(ffprobe, src, settings, translator, _note)
            except TranslateError as exc:
                skip(index, src, src, f"翻译失败，已跳过：{exc}")
                continue
            if stem:
                new_name = stem + ".mp3"

        if rename_only:
            # 只重命名：复制到输出目录，原文件保持不动
            assert out_dir is not None  # rename 模式已要求输出目录
            base = new_name or src.name
            if target.is_dir():
                dst = out_dir / src.parent.relative_to(target) / base
            else:
                dst = out_dir / base
            if dst.exists() and not cfg.overwrite:
                skip(index, src, dst, "目标文件已存在（加 --overwrite 可覆盖）")
                continue
            if dst in planned:
                skip(index, src, dst, "与本次其它文件重名")
                continue
            planned.add(dst)
            tasks.append((index, src, dst, "copy"))
            continue

        base = new_name or src.name
        if out_dir is None:
            dst = src.parent / base
        elif target.is_dir():
            dst = out_dir / src.parent.relative_to(target) / base
        else:
            dst = out_dir / base

        if dst != src and dst.exists() and not cfg.overwrite:
            skip(index, src, dst, "目标文件已存在（加 --overwrite 可覆盖）")
            continue
        if dst in planned:
            skip(index, src, dst, "与本次其它文件重名")
            continue
        planned.add(dst)
        tasks.append((index, src, dst, "encode"))

    if translator is not None:
        translator.save_cache()

    workers = max(1, min(cfg.jobs, max(1, len(tasks))))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_task, ctx, idx, src, dst, mode): (idx, src, dst)
                for idx, src, dst, mode in tasks
            }
            for fut in as_completed(futures):
                idx, src, dst = futures[fut]
                status, err = fut.result()
                if status == "ok":
                    summary.ok += 1
                    emit("file_ok", index=idx, total=total, path=str(src), dst=str(dst))
                else:
                    summary.failed.append((src, err))
                    emit("file_fail", index=idx, total=total, path=str(src), error=err)
                emit("file_done", index=idx, total=total, status=status)
    except KeyboardInterrupt:
        raise ToolError("已中断。")

    summary.seconds = time.monotonic() - started
    emit("done", summary=summary, total=total)
    return summary
