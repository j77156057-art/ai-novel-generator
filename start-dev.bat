@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  AI 小说生成器 - 一键启动
echo ============================================
echo.

if not exist "backend\venv\Scripts\python.exe" (
  echo [错误] 找不到 backend\venv\Scripts\python.exe
  echo        请先在 backend 下建虚拟环境并安装依赖。
  pause
  exit /b 1
)
if not exist "frontend\node_modules" (
  echo [错误] 找不到 frontend\node_modules
  echo        请先在 frontend 下执行 npm install。
  pause
  exit /b 1
)

echo [1/2] 启动后端  http://127.0.0.1:8000  ...
start "novel-backend :8000" cmd /k "cd /d "%~dp0backend" && venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000"

timeout /t 5 /nobreak >nul

echo [2/2] 启动前端  http://localhost:5173  ...
start "novel-frontend :5173" cmd /k "cd /d "%~dp0frontend" && npm run dev"

echo.
echo 等待前端编译完成后会尝试自动打开浏览器...
timeout /t 8 /nobreak >nul
start "" "http://localhost:5173"

echo.
echo 已启动。关闭这两个黑窗口即停止服务。
echo   前端界面：http://localhost:5173
echo   后端接口文档：http://127.0.0.1:8000/docs
echo.
pause
