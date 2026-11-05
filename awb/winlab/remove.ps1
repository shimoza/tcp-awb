# awb-winlab removal for lab machine @@MACHINE@@ (Architect Workbench, task WL part 2).
#
# Printed by `awb winlab remove @@MACHINE@@` on the Workbench host. Run it on the lab laptop, as administrator, in
# Windows PowerShell 5.1:
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\remove.ps1
#
# It removes what setup.ps1 made, each step checks its own result and stops with one line naming the step:
#   - the scheduled task awb-winlab-tunnel and its ssh process
#   - the scheduled tasks of the jobs a project session started (task folder \awb-winlab\, WL part 3)
#   - the sshd service (the OpenSSH server when setup installed it; else the sshd_config and start type from before)
#   - the keys: administrators_authorized_keys (or the one from before), the host key setup made, the tunnel key and
#     known_hosts
#   - the local account %ProgramData%\awb-winlab\state.txt names (setup made it), its processes and its profile
#     folder
#   - %ProgramData%\awb-winlab
# Without state.txt setup changed nothing outside its folder. A second run finds nothing left and ends with done.

$ErrorActionPreference = 'Stop'

$Machine = '@@MACHINE@@'
$TaskName = 'awb-winlab-tunnel'
$JobTaskPath = '\awb-winlab\'

$SshDir = Join-Path $env:ProgramData 'ssh'
$Dir = Join-Path $env:ProgramData 'awb-winlab'
$StateFile = Join-Path $Dir 'state.txt'
$SshdConfig = Join-Path $SshDir 'sshd_config'
$HostKey = Join-Path $SshDir 'ssh_host_ed25519_key'
$AuthorizedKeys = Join-Path $SshDir 'administrators_authorized_keys'
$TunnelKey = Join-Path $Dir 'tunnel_ed25519'
$KnownHosts = Join-Path $Dir 'known_hosts'
$AdminsSid = 'S-1-5-32-544'


# ------------------------------------------------------------------------------------------------- helpers

function Stop-Removal([string]$Step, [string]$Why) {
    $line = ($Why -replace '\s+', ' ').Trim()
    Write-Host ('awb-winlab remove: stopped at step {0}: {1}' -f $Step, $line)
    exit 1
}

# One step: $Do changes the laptop, $Check reads the result back and must end with $true. Anything else stops the
# script with one line naming the step.
function Step([string]$Name, [scriptblock]$Do, [scriptblock]$Check) {
    try { & $Do | Out-Null } catch { Stop-Removal $Name $_.Exception.Message }
    $result = $null
    try { $result = @(& $Check) } catch { Stop-Removal $Name $_.Exception.Message }
    if ($result.Count -eq 0 -or $result[-1] -ne $true) { Stop-Removal $Name 'its result check failed' }
    Write-Host ('awb-winlab remove: {0} ok' -f $Name)
}

function Test-Administrator {
    $principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([System.Security.Principal.SecurityIdentifier]$AdminsSid)
}

function Get-State([string]$Key) {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $StateFile) {
        if ($line -like ($Key + '=*')) { return $line.Substring($Key.Length + 1) }
    }
    return $null
}

function Get-SshdCapability {
    return @(Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' }) | Select-Object -First 1
}

function Get-TunnelProcess {
    return @(Get-CimInstance -ClassName Win32_Process -Filter "Name='ssh.exe'" |
             Where-Object { $_.CommandLine -and $_.CommandLine.Contains('awb-winlab') -and $_.CommandLine.Contains('tunnel_ed25519') })
}

function Get-AccountProcess([string]$Sid) {
    if (-not $Sid) { return @() }
    return @(Get-CimInstance -ClassName Win32_Process | Where-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid -ErrorAction SilentlyContinue
        $owner -and $owner.Sid -eq $Sid })
}

function Remove-IfThere([string]$Path) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

function Test-SameFile([string]$Path, [string]$Copy) {
    if (-not (Test-Path -LiteralPath $Copy)) { return -not (Test-Path -LiteralPath $Path) }
    return (Test-Path -LiteralPath $Path) -and ((Get-FileHash -LiteralPath $Path).Hash -eq (Get-FileHash -LiteralPath $Copy).Hash)
}


# ------------------------------------------------------------------------------------------------- the steps

Step 'administrator' {
    if (-not (Test-Administrator)) { throw 'run it in a PowerShell window opened as administrator' }
} {
    Test-Administrator
}

# What setup found before its first run; without state.txt setup changed nothing outside its folder.
# setup refuses an account name that exists before its first run, so the account state.txt names is its own.
$HaveState = Test-Path -LiteralPath $StateFile
$StateMachine = Get-State 'machine'
$Account = Get-State 'account'
$AccountSid = Get-State 'account_sid'
if ($Account -notmatch '^[a-z][a-z0-9-]{1,19}$') { $Account = $null }
if ($Account -and -not $AccountSid) {
    $user = Get-LocalUser -Name $Account -ErrorAction SilentlyContinue
    if ($user) { $AccountSid = $user.SID.Value }
}
# only a well formed account name makes a profile folder to remove, never C:\Users itself
$AccountFolder = $null
if ($Account) { $AccountFolder = Join-Path $env:SystemDrive ('Users\' + $Account) }
$OpenSshMade = (Get-State 'openssh_before') -eq 'false'
$HostKeyMade = (Get-State 'host_key_before') -eq 'false'
$StartBefore = Get-State 'sshd_start_before'
$ConfigBefore = Join-Path $Dir 'sshd_config.before'
$KeysBefore = Join-Path $Dir 'administrators_authorized_keys.before'

Step 'state' {
    if ($StateMachine -and $StateMachine -ne $Machine) {
        throw ('this laptop is set up as lab machine {0}, not {1}: print the removal script for {0}' -f $StateMachine, $Machine)
    }
} {
    -not $StateMachine -or $StateMachine -eq $Machine
}

Step 'tunnel-task' {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($task) {
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\'
        Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    }
    foreach ($p in Get-TunnelProcess) { Stop-Process -Id $p.ProcessId -Force }
} {
    -not (Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue) -and (Get-TunnelProcess).Count -eq 0
}

Step 'job-tasks' {
    foreach ($task in @(Get-ScheduledTask -TaskPath $JobTaskPath -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName $task.TaskName -TaskPath $JobTaskPath -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $JobTaskPath -Confirm:$false
    }
    $service = New-Object -ComObject Schedule.Service
    $service.Connect()
    try { $service.GetFolder('\').DeleteFolder($JobTaskPath.Trim('\'), 0) } catch { }
} {
    @(Get-ScheduledTask -TaskPath $JobTaskPath -ErrorAction SilentlyContinue).Count -eq 0
}

Step 'account-processes' {
    foreach ($p in Get-AccountProcess $AccountSid) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
} {
    (Get-AccountProcess $AccountSid).Count -eq 0
}

Step 'sshd-service' {
    if ($HaveState -and (Get-Service -Name sshd -ErrorAction SilentlyContinue)) {
        Stop-Service -Name sshd -Force
        if ($OpenSshMade) {
            $cap = Get-SshdCapability
            if ($cap -and $cap.State -eq 'Installed') { Remove-WindowsCapability -Online -Name $cap.Name | Out-Null }
        } else {
            if (Test-Path -LiteralPath $ConfigBefore) { Copy-Item -LiteralPath $ConfigBefore -Destination $SshdConfig -Force }
            else { Remove-IfThere $SshdConfig }
            if ($StartBefore -and $StartBefore -ne 'none') { Set-Service -Name sshd -StartupType $StartBefore }
            if ($StartBefore -eq 'Automatic') { Start-Service -Name sshd }
        }
    }
} {
    if (-not $HaveState) { $true }
    elseif ($OpenSshMade) { $cap = Get-SshdCapability; -not $cap -or $cap.State -ne 'Installed' }
    else { Test-SameFile $SshdConfig $ConfigBefore }
}

Step 'keys' {
    if ($HaveState) {
        if (Test-Path -LiteralPath $KeysBefore) { Copy-Item -LiteralPath $KeysBefore -Destination $AuthorizedKeys -Force }
        else { Remove-IfThere $AuthorizedKeys }
        if ($HostKeyMade) {
            Remove-IfThere $HostKey
            Remove-IfThere ($HostKey + '.pub')
        }
        if ($OpenSshMade) { Remove-IfThere $SshDir }
    }
    Remove-IfThere $TunnelKey
    Remove-IfThere ($TunnelKey + '.pub')
    Remove-IfThere $KnownHosts
} {
    $hostKeyGone = -not $HostKeyMade -or -not (Test-Path -LiteralPath $HostKey)
    $own = -not (Test-Path -LiteralPath $TunnelKey) -and -not (Test-Path -LiteralPath $KnownHosts)
    (-not $HaveState -or (Test-SameFile $AuthorizedKeys $KeysBefore)) -and $hostKeyGone -and $own
}

Step 'profile' {
    if ($AccountSid) {
        $userProfile = Get-CimInstance -ClassName Win32_UserProfile -Filter ("SID='{0}'" -f $AccountSid)
        if ($userProfile) { Remove-CimInstance -InputObject $userProfile }
    }
    if ($AccountFolder) { Remove-IfThere $AccountFolder }
} {
    (-not $AccountSid -or -not (Get-CimInstance -ClassName Win32_UserProfile -Filter ("SID='{0}'" -f $AccountSid))) -and
        (-not $AccountFolder -or -not (Test-Path -LiteralPath $AccountFolder))
}

Step 'account' {
    if ($AccountSid -and (Get-LocalUser -SID $AccountSid -ErrorAction SilentlyContinue)) { Remove-LocalUser -SID $AccountSid }
} {
    -not $AccountSid -or -not (Get-LocalUser -SID $AccountSid -ErrorAction SilentlyContinue)
}

Step 'state-folder' {
    Remove-IfThere $Dir
} {
    -not (Test-Path -LiteralPath $Dir)
}

Write-Host ('awb-winlab remove: done, nothing of lab machine {0} is left on this laptop' -f $Machine)
Write-Host ('If awb winlab list on the Workbench host still lists it: awb winlab remove {0}' -f $Machine)
