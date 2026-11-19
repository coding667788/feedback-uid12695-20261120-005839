@echo off
REM Windows 原生测试入口：仅依赖标准库，无需安装任何第三方包
cd /d "%~dp0"
python -I -m unittest discover -s tests -v
