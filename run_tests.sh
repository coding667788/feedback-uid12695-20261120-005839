#!/usr/bin/env bash
# 测试入口（Git Bash / 类 Unix）：仅依赖标准库
set -euo pipefail
cd "$(dirname "$0")"
python -m unittest discover -s tests -v
