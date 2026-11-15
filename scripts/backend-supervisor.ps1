<#
.SYNOPSIS
  后端守护：拉起 uvicorn，进程被杀/崩溃后自动重启，运行日志按天落到 logs\。

.DESCRIPTION
  只做三件事：端口占用检查、循环拉起、日志追加。不装 NSSM / systemd / 任何运维框架。

  重要：不要用 Start-Process / 当前终端直接起本脚本做“常驻”。
  这类子进程会挂在启动它的终端/会话上，那个会话一退，守护和 uvicorn 会被一起回收，
  表现为日志停在“Uvicorn running”、没有退出原因、端口悄悄消失。
  2026-11-15 实测：WMI Win32_Process.Create 同样会被整树回收，只有计划任务可靠。
  常驻请用同目录的 install-backend-task.ps1 注册计划任务（会话无关、开机自启）。

  用法（在仓库根执行）：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1 -Port 8000
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\backend-supervisor.ps1 -NoRestart   # 只跑一次（排障用）

  日志：logs\backend-<端口>-<yyyyMMdd>.log（已写入 .gitignore 的 *.log）
  本脚本只写文件日志，不依赖控制台；控制台被关掉不会影响它继续跑。

  注意：不做 --reload。改代码后需要由本脚本重启进程，避免 reload 的多进程模型与
  单例缓存、后台线程互相打架。
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [string]$LogDir = "",
    [int]$RestartDelaySec = 3,
    # 可选：临时覆盖数据库地址（自测/隔离环境用），不传就走 backend\.env
    [string]$DatabaseUrl = "",
    [switch]$NoRestart
)

$ErrorActionPreference = "Continue"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
if ($DatabaseUrl) { $env:DATABASE_URL = $DatabaseUrl }

$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python = Join-Path $backend "venv\Scripts\python.exe"

function Write-Log([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    try { Add-Content -Path $logFile -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
    # 控制台可能已经不存在（被计划任务/会话接管），写不出去也不能让循环挂掉。
    try { Write-Host $line } catch { }
}

if (-not (Test-Path $python)) {
    $msg = "[错误] 找不到 $python，请先在 backend 下建虚拟环境并安装依赖。"
    Write-Host $msg -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $backend "app\main.py"))) {
    Write-Host "[错误] 找不到 backend\app\main.py，仓库结构不完整。" -ForegroundColor Red
    exit 1
}

if (-not $LogDir) { $LogDir = Join-Path $root "logs" }
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$logFile = Join-Path $LogDir ("backend-{0}-{1}.log" -f $Port, (Get-Date -Format "yyyyMMdd"))

# 端口预检：已经在跑就不要起第二个（两个实例会抢同一个 sqlite 与后台 worker）。
try {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse($BindHost), $Port)
    $listener.Start()
    $listener.Stop()
} catch {
    Write-Log "[错误] 端口 $Port 已被占用（可能后端已在运行）。本脚本不重复拉起，请先确认。"
    exit 2
}

Write-Log "守护启动：supervisor_pid=$PID python=$python port=$Port db=$(if ($DatabaseUrl) { $DatabaseUrl } else { '(backend/.env)' })"

$delay = $RestartDelaySec
while ($true) {
    $startedAt = Get-Date
    Write-Log "拉起 uvicorn app.main:app --host $BindHost --port $Port"

    Push-Location $backend
    try {
        & $python -m uvicorn app.main:app --host $BindHost --port $Port 2>&1 |
            ForEach-Object { Write-Log $_ }
        $code = $LASTEXITCODE
    } catch {
        # 管道/写日志异常也不能把守护带走，记下来继续下一轮。
        $code = -1
        Write-Log "拉取进程输出时异常：$($_.Exception.Message)"
    } finally {
        Pop-Location
    }

    $aliveSec = [int]((Get-Date) - $startedAt).TotalSeconds
    if ($NoRestart) {
        Write-Log "进程退出（exit=$code，存活 ${aliveSec}s）；-NoRestart 生效，不再拉起。"
        break
    }

    # 启动即崩（<60s）按指数退避，避免坏配置把日志刷爆；跑稳过一轮就回到基础间隔。
    # 注意：退避只影响“多久后再拉”，不会放弃重启——守护不会因为连续失败而静默退出。
    if ($aliveSec -lt 60) {
        $delay = [Math]::Min($delay * 2, 60)
        Write-Log "进程在 ${aliveSec}s 后退出（不足 60s），按启动不稳定处理，退避到 ${delay}s 后继续重试（不会停止）。exit=$code"
    } else {
        $delay = $RestartDelaySec
        Write-Log "进程退出（exit=$code，存活 ${aliveSec}s），${delay}s 后重启。"
    }
    Start-Sleep -Seconds $delay
}
