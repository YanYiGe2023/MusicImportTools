#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入口脚本：实际实现位于 mp3tools 包（见 README「代码结构」）。

    python volume_tool.py            # 打开图形界面
    python volume_tool.py --help     # 查看命令行参数
"""

from __future__ import annotations

import sys


def main() -> int:
    from mp3tools.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
