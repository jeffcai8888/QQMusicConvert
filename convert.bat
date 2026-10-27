@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 python, 请先安装 Python 3.8+ 并加入 PATH。
  pause
  exit /b 1
)

if "%~1"=="" (
  echo 用法: 把 .mflac / .mgg / .qmc 文件或文件夹拖到本脚本上即可转换。
  echo 或直接输入路径:
  set /p INPUT=路径:
  if "%INPUT%"=="" pause & exit /b 0
  python mflac2mp3.py "%INPUT%"
) else (
  python mflac2mp3.py %*
)

echo.
pause
