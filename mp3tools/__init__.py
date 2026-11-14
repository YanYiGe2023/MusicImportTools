"""MP3 批量处理工具。

包结构：
    config     设置模型与持久化
    errors     通用异常
    media      ffmpeg/ffprobe 定位、探测与编码参数
    translate  百度翻译客户端与字符检测
    naming     输出文件名生成
    batch      批量任务与音量处理
    cli        命令行入口
    gui        图形界面
"""

from .config import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION"]
