"""ffmpeg / ffprobe 定位、媒体探测与编码参数。"""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import subprocess
from pathlib import Path

from .config import APP_DIR

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0
SUBPROCESS_FLAGS = {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


def _registry_path_dirs() -> list[str]:
    """从注册表读取最新的 PATH（当前进程的环境变量可能是旧的）。"""
    if not IS_WIN:
        return []
    dirs: list[str] = []
    try:
        import winreg
    except ImportError:
        return []
    for hive, sub in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
            if isinstance(value, str):
                dirs.extend(p for p in value.split(";") if p.strip())
        except OSError:
            continue
    return dirs


def _extra_dirs() -> list[str]:
    dirs: list[str] = []
    if not IS_WIN:
        return dirs
    dirs.extend(_registry_path_dirs())
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
        dirs.extend(
            glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*ffmpeg*", "**", "bin"), recursive=True)
        )
    dirs.extend([r"C:\ffmpeg\bin", r"C:\ProgramData\chocolatey\bin", r"C:\Program Files\ffmpeg\bin"])
    return list(dict.fromkeys(dirs))


def find_binary(name: str) -> str | None:
    """在环境变量、PATH、脚本同目录 bin、注册表 PATH 中查找可执行文件。"""
    exe = name + (".exe" if IS_WIN else "")

    env_value = os.environ.get(name.upper() + "_BIN")
    if env_value:
        p = Path(env_value)
        if p.is_dir():
            p = p / exe
        if p.is_file():
            return str(p)

    found = shutil_which(name)
    if found:
        return found

    local_bin = APP_DIR / "bin" / exe
    if local_bin.is_file():
        return str(local_bin)

    for d in _extra_dirs():
        p = Path(d) / exe
        if p.is_file():
            return str(p)
    return None


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def resolve_tools() -> tuple[str, str | None]:
    ffmpeg = find_binary("ffmpeg")
    if not ffmpeg:
        raise ToolError(
            "未找到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
            "或把 ffmpeg.exe 放到本脚本旁的 bin 文件夹里。"
        )
    ffprobe = find_binary("ffprobe")
    if not ffprobe:
        sibling = Path(ffmpeg).with_name("ffprobe" + (".exe" if IS_WIN else ""))
        ffprobe = str(sibling) if sibling.is_file() else None
    return ffmpeg, ffprobe


# --------------------------------------------------------------------------
# 音频探测与过滤表达式
# --------------------------------------------------------------------------

def probe_audio(ffprobe: str | None, path: Path) -> tuple[float, int]:
    """返回 (时长秒, 源比特率 bps)。探测失败时返回 (0, 0)。"""
    if not ffprobe:
        return 0.0, 0
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration,bit_rate",
        "-of", "json",
        str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            **SUBPROCESS_FLAGS,
        )
        if out.returncode != 0:
            return 0.0, 0
        data = json.loads(out.stdout or "{}")
        fmt = data.get("format") or {}
        duration = float(fmt.get("duration") or 0)
        bitrate = int(float(fmt.get("bit_rate") or 0))
        return duration, bitrate
    except Exception:
        return 0.0, 0


def read_tags(ffprobe: str | None, path: Path) -> dict[str, str]:
    """用 ffprobe 读取 ID3 的标题、歌手标签。"""
    if not ffprobe:
        return {}
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format_tags=title,artist",
        "-of", "json",
        str(path),
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            **SUBPROCESS_FLAGS,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout or "{}")
        raw = (data.get("format") or {}).get("tags") or {}
        return {str(k).lower(): str(v).strip() for k, v in raw.items() if str(v).strip()}
    except Exception:
        return {}


def build_filter(db: float | None, percent: float | None) -> str:
    if db is not None:
        # db 是「降低多少分贝」，滤镜里必须是负值
        return f"volume={-abs(db):g}dB"
    if percent is not None:
        return f"volume={percent / 100.0:.6g}"
    raise ToolError("没有指定音量数值。")


def percent_to_db(percent: float) -> float:
    """把「降到原来的百分比」换算成需要降低的正分贝数。"""
    return 20.0 * math.log10(100.0 / percent)


def db_to_percent(db: float) -> float:
    return 100.0 * (10.0 ** (-db / 20.0))


def audio_codec_args(bitrate: str) -> list[str]:
    """默认用 VBR（约 190 kbps，音量精确、体积适中）；也可指定固定比特率。

    注意：CBR 192k 时 libmp3lame 会自带约 0.27 dB 衰减，所以固定码率时避开 192k。
    """
    if bitrate == "auto":
        return ["-c:a", "libmp3lame", "-q:a", "2"]
    return ["-c:a", "libmp3lame", "-b:a", bitrate]
