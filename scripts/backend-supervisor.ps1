<#
.SYNOPSIS
  后端守护：拉起 uvicorn，进程被杀/崩溃后自动重启，运行日志按天落到 logs\。

.DESCRIPTION
  只做三件事：端口占用检查、循环拉起、日志追加。不装 NSSM / systemd / 任何运维框架。

  用法（在仓库根执行）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1 -Port 8000
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1 -NoRestart   # 只跑一次（排障用）

  日志：logs\backend-<端口>-<yyyyMMdd>.log（已写入 .gitignore 的 *.log）
  开机自启（可选，不自动执行）：
    schtasks /Create /TN "AI-Novel-Backend" /SC ONLOGON /RL LIMITED ^
      /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"<仓库绝对路径>\scripts\backend-supervisor.ps1\""

  注意：不做 --reload。改代码后需要由本脚本重启进程，避免 reload 的多进程模型与
  单例缓存、后台线程互相打架。要停止服务请直接关掉本窗口（Ctrl+C 会被守护逻辑
  当成一次“进程退出”，随后又拉起来）。
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [string]$LogDir = "",
    [int]$RestartDelaySec = 3,
    [switch]$NoRestart
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python = Join-Path $backend "venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "[错误] 找不到 $python，请先在 backend 下建虚拟环境并安装依赖。" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $backend "app\main.py"))) {
    Write-Host "[错误] 找不到 backend\app\main.py，仓库结构不完整。" -ForegroundColor Red
    exit 1
}

if (-not $LogDir) { $LogDir = Join-Path $root "logs" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$logFile = Join-Path $LogDir ("backend-{0}-{1}.log" -f $Port, (Get-Date -Format "yyyyMMdd"))

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

# 端口预检：已经在跑就不要起第二个（两个实例会抢同一个 sqlite 与后台 worker）。
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($BindHost), $Port)
    $listener.Start()
    $listener.Stop()
} catch {
    Write-Host "[错误] 端口 $Port 已被占用（可能后端已在运行）。本脚本不重复拉起，请先确认。" -ForegroundColor Red
    exit 2
}

# Uvicorn 自己的日志带时间；这里再给每行加上“什么时候由守护脚本转出来的”，
# 便于把“进程什么时候死的”和“错误长什么样”对上。
Write-Log "守护启动：python=$python port=$Port log=$logFile"

$delay = $RestartDelaySec
while ($true) {
    $startedAt = Get-Date
    Write-Log "拉起 uvicorn app.main:app --host $BindHost --port $Port"

    Push-Location $backend
    try {
        & $python -m uvicorn app.main:app --host $BindHost --port $Port 2>&1 |
            ForEach-Object { Write-Log $_ }
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    $aliveSec = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($NoRestart) {
        Write-Log "进程退出（exit=$code，存活 ${aliveSec}s）；-NoRestart 生效，不再拉起。"
        break
    }

    # 启动即崩（<60s）按指数退避，避免坏配置把日志刷爆；跑稳过一轮就回到基础间隔。
    if ($aliveSec -lt 60) {
        $delay = [Math]::Min($delay * 2, 60)
    } else {
        $delay = $RestartDelaySec
    }
    Write-Log "进程退出（exit=$code，存活 ${aliveSec}s），${delay}s 后重启。"
    Start-Sleep -Seconds $delay
}
