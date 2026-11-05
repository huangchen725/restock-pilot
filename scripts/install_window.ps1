# Thin ordinary-user window. All installation logic remains in setup.ps1.
param([switch]$Unattended, [string]$CacheDirectory = '')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot -Parent
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path $PSScriptRoot 'owned_process.ps1')
$script:worker=$null
$script:success=$false
$script:result=1
$log=Join-Path $root ('outputs/install-window/'+[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss-fffffff')+'.log')
New-Item -ItemType Directory -Force -Path (Split-Path $log -Parent) | Out-Null
$form=New-Object Windows.Forms.Form
$form.Text='RestockPilot 安装 / 修复'
$form.Size=New-Object Drawing.Size(680,320)
$form.StartPosition='CenterScreen'
$label=New-Object Windows.Forms.Label
$label.Location=New-Object Drawing.Point(24,24)
$label.Size=New-Object Drawing.Size(620,155)
$label.Text="将使用当前锁定版本安装或修复项目环境。已有配置和业务数据保留。`n首次安装需要网络；无需管理员权限。请先停止本项目服务。"
$form.Controls.Add($label)
$button=New-Object Windows.Forms.Button
$button.Text='安装 / 修复环境'
$button.Location=New-Object Drawing.Point(24,200)
$button.Size=New-Object Drawing.Size(220,40)
$form.Controls.Add($button)
$cancel=New-Object Windows.Forms.Button
$cancel.Text='关闭'
$cancel.Location=New-Object Drawing.Point(480,200)
$cancel.Size=New-Object Drawing.Size(140,40)
$form.Controls.Add($cancel)
$cancel.Add_Click({$form.Close()})
function Begin-Install {
    if ($script:worker) {return}
    $exe=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'setup.ps1')+'" -NoPause'
    if ($CacheDirectory) {$arguments+=' -CacheDirectory "'+$CacheDirectory+'"'}
    $script:worker=New-Object RestockOwnedProcess($exe,$arguments,$root,$log)
    $button.Enabled=$false
    $cancel.Text='取消安装'
    $label.Text="正在安装并核对运行环境，请稍候。已完成的步骤会在再次安装时复用。`n取消会停止本次安装进程，已有配置不会被覆盖。`n诊断记录：$log"
}
function Check-Install {
    if ($script:worker -and $script:worker.Done) {
        $script:result=$script:worker.ExitCode
        $script:worker.Dispose();$script:worker=$null
        $button.Enabled=$true;$cancel.Text='关闭'
        if ($script:result -eq 0) {
            $script:success=$true;$button.Text='完成并打开启动窗口'
            $label.Text='安装完成，当前锁定运行环境已核验。打开启动窗口后选择模式并填写模型密钥。已有配置已保留。'
        } else {
            $label.Text="安装未完成，现有配置保留。请先停止正在运行的服务，并检查网络或安装诊断后重试。`n诊断记录：$log"
        }
    }
}
$button.Add_Click({
    try {
        if ($script:success) {
            $exe=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
            Start-Process -FilePath $exe -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $PSScriptRoot 'launch.ps1')+'" -Action Launch') -WorkingDirectory $root -WindowStyle Hidden
            $form.Close()
        } else {Begin-Install}
    } catch {$label.Text='无法开始安装，请检查项目写权限和诊断记录。';$button.Enabled=$true}
})
$form.Add_FormClosing({
    if ($script:worker) {
        if (-not $Unattended -and [Windows.Forms.MessageBox]::Show('取消本次安装？下次可以重新安装，已有配置保留。','停止安装',[Windows.Forms.MessageBoxButtons]::OKCancel) -ne [Windows.Forms.DialogResult]::OK) {$_.Cancel=$true;return}
        $script:worker.Dispose();$script:worker=$null
    }
})
$timer=New-Object Windows.Forms.Timer
$timer.Interval=400
$timer.Add_Tick({Check-Install})
try {
    if ($Unattended) {
        Begin-Install
        while ($script:worker) {Check-Install;Start-Sleep -Milliseconds 100}
    } else {
        $timer.Start();[void]$form.ShowDialog()
    }
} finally {
    $timer.Stop();$timer.Dispose()
    if ($script:worker) {$script:worker.Dispose()}
    $form.Dispose()
}
exit $script:result
