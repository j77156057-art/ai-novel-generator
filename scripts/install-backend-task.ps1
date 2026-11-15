<#
.SYNOPSIS
  把后端守护注册成 Windows 计划任务（会话无关、开机自启、崩溃后由守护自动拉起）。

.DESCRIPTION
  为什么必须用计划任务（2026-11-15 踩过的坑，别再绕一圈）：
  工具会话的进程树会被整体回收，Start-Process 与 WMI Win32_Process.Create 都
  不足以脱离——两者起的守护会在发起命令返回后一起消失，日志停在
  "Uvicorn running"、没有退出原因、端口悄悄变空。只有计划任务可靠：
  它由 Task Scheduler 服务托管，和发起命令的会话/终端无父子关系。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-backend-task.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-backend-task.ps1 -Port 8000
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-backend-task.ps1 -Port 8011 -TaskName AiNovelBackendTest -DatabaseUrl "sqlite:///backend/backups/probe.db"

  任务名：默认 AiNovelBackend-<端口>（-TaskName 可改），如 AiNovelBackend-8000。

  卸载（顺序很重要，别只用 Stop-ScheduledTask 就以为停了）：
    Stop-ScheduledTask    -TaskName "AiNovelBackend-8000"
    # 2026-11-15 实测：Stop-ScheduledTask 只结束任务动作进程（powershell 守护），
    # **不连带结束它派生的 venv shim 与 uvicorn**——两者变成孤儿进程继续监听端口、
    # 继续攥着 sqlite。端口因此不会释放，紧接着 Start 会因端口预检直接 exit 2。
    # 所以停任务后必须按 PID 手动收子进程，且**先 uvicorn 后 shim**（反过来 shim
    # 退出时可能把子进程留下，或子进程重新监听）：
    #   $uvicorn = (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess
    #   Stop-Process -Id $uvicorn          # 先：真正监听端口的 uvicorn
    #   $shim = (Get-CimInstance Win32_Process -Filter "ProcessId=$uvicorn").ParentProcessId
    #   Stop-Process -Id $shim             # 后：venv\Scripts\python.exe 启动器 shim
    #   再用 @(Get-NetTCPConnection -LocalPort 8000 -State Listen).Count -eq 0 确认端口已释放
    Unregister-ScheduledTask -TaskName "AiNovelBackend-8000" -Confirm:$false

  排障：
    1) 看任务最后一次运行结果（0 成功；0x1/0x2/0x41301 等见下）：
         Get-ScheduledTask -TaskName "AiNovelBackend-8000" | Get-ScheduledTaskInfo |
             Select TaskName, State, LastRunTime, LastTaskResult, NextRunTime
    2) LastTaskResult = 0x41301：任务仍在运行（正常常驻态），不是失败。
    3) LastTaskResult = 2（或 0x2）：多半是「端口被占时 exit 2」——守护脚本开头
       做 TcpListener 预检，发现端口已被监听就直接 exit 2，不会重复拉起第二个
       实例（两个实例会抢同一个 sqlite 与后台 worker）。此时先确认是谁在听：
         Get-NetTCPConnection -LocalPort 8000 -State Listen | Select OwningProcess
       要么结束旧实例后 Start-ScheduledTask，要么改用空闲端口。
    4) LastTaskResult = 1：找不到 backend\venv\Scripts\python.exe 或
       backend\app\main.py（仓库结构/虚拟环境缺失）。
    5) 进程反复重启、但退出原因不明：看 logs\backend-<端口>-<yyyyMMdd>.log，
       守护会记下每轮 exit code 与存活秒数。
    6) 任务已是 Ready/已删，但端口还在听：这是 Stop-ScheduledTask 不连带清理子
       进程的必然结果（见上面「卸载」）。按 uvicorn → shim 的顺序杀这两个 PID，
       再用 Get-NetTCPConnection 复查为 0，才算真的停了。只 stop 任务不算。
#>
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$TaskName = "",
    [string]$DatabaseUrl = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sup = Join-Path $PSScriptRoot "backend-supervisor.ps1"
if (-not (Test-Path $sup)) { throw "找不到 $sup" }
if (-not $TaskName) { $TaskName = "AiNovelBackend-$Port" }

$arg = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Port {1}' -f $sup, $Port
if ($DatabaseUrl) { $arg += ' -DatabaseUrl "{0}"' -f $DatabaseUrl }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "AI 小说生成后端守护：uvicorn :$Port，崩溃自动拉起，日志见 logs\\" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(90)
$pid8000 = $null
while ((Get-Date) -lt $deadline) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($c) { $pid8000 = $c[0].OwningProcess; break }
    Start-Sleep -Seconds 1
}

$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
"任务：$TaskName（状态 $((Get-ScheduledTask -TaskName $TaskName).State)）"
"最后运行时间：$($info.LastRunTime)"
if ($pid8000) { "监听 PID：$pid8000（端口 $Port）" } else { "端口 $Port 在 90s 内未起来，请查 logs\backend-$Port-*.log" }
