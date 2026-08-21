# ClaudeDeck 开机自启(可选功能): .\autostart.ps1 -Enable | -Disable | -Status
# 用 ScheduledTasks cmdlet 而非 schtasks.exe:后者设不了工作目录,而 -m app.tray 依赖 cwd。
param(
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Status
)
$ErrorActionPreference = "Stop"
$TaskName = "ClaudeDeckTray"
$Root = $PSScriptRoot
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"

if ($Enable) {
    if (-not (Test-Path $Pythonw)) { throw "venv 不存在,先跑一次 start_claudedeck.bat" }
    # 不直接执行 pythonw:部分 uv 版本的 venv 垫片是控制台子系统,会带出黑窗。
    # 统一用 Start-Process -WindowStyle Hidden 包一层,两种垫片都无窗。
    $inner = "Start-Process -WindowStyle Hidden -FilePath '$Pythonw' -ArgumentList '-m','app.tray' -WorkingDirectory '$Root'"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -Command `"$inner`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "[ClaudeDeck] 开机自启已启用(计划任务 $TaskName,登录时静默拉起托盘)。"
} elseif ($Disable) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        Write-Host "[ClaudeDeck] 开机自启已移除。"
    } catch {
        Write-Host "[ClaudeDeck] 本来就没有启用。"
    }
} else {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) { Write-Host "[ClaudeDeck] 自启已启用,状态: $($t.State)" }
    else { Write-Host "[ClaudeDeck] 自启未启用。启用: .\autostart.ps1 -Enable" }
}
