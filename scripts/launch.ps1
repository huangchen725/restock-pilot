param([ValidateSet('Launch','Install','Stop')][string]$Action='Launch')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
$python=Join-Path $root '.venv/Scripts/pythonw.exe'
try {
    if ($Action -eq 'Install' -or -not (Test-Path -LiteralPath $python)) {
        & (Join-Path $PSScriptRoot 'install_window.ps1')
    } else {
        $args='"'+(Join-Path $root 'desktop.py')+'"'
        if ($Action -eq 'Stop') { $args+=' --stop' }
        Start-Process -FilePath $python -ArgumentList $args -WorkingDirectory $root -WindowStyle Hidden
    }
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [void][Windows.Forms.MessageBox]::Show('无法打开 RestockPilot。请使用“安装或修复 RestockPilot”入口检查环境。','RestockPilot')
}
