@echo off
chcp 65001 >nul
cd /d "%~dp0"
title histmap 工作台

echo.
echo   histmap 历史地图工作台
echo   ----------------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo   [x] 找不到 python。
  echo       装一个 Python 3.10+ 并勾选 "Add to PATH"，然后重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo   正在检查依赖...
python -c "import fastapi, uvicorn, PIL, shapely, numpy, zhconv" 2>nul
if errorlevel 1 (
  echo   [!] 有依赖没装，现在装一遍（第一次会慢一点）
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo   [x] 依赖安装失败，请把上面的报错发出来。
    pause
    exit /b 1
  )
)

echo   启动中，浏览器会自动打开...
echo   关掉这个黑窗口就是停止服务。
echo.
python packages\server\histmap_server\app.py --open

echo.
echo   服务已停止。
pause
