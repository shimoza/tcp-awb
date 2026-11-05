# awb-winlab setup for lab machine @@MACHINE@@ (Architect Workbench, task WL part 2).
#
# Printed by `awb winlab bootstrap @@MACHINE@@` on the Workbench host. Run it once on the lab laptop, as
# administrator, in Windows PowerShell 5.1:
#
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
#
# What it does, each step checks its own result and stops with one line naming the step:
#   - installs the OpenSSH server and lets it listen on 127.0.0.1 port 22 only, key logon only
#   - creates the local account @@ACCOUNT@@ (Administrators, Hyper-V Administrators) with a random password nobody
#     keeps; the Workbench host logs on with its key only
#   - writes the host's public key to %ProgramData%\ssh\administrators_authorized_keys
#   - generates this laptop's own tunnel key and a visible scheduled task awb-winlab-tunnel that starts at boot and
#     holds one outgoing SSH connection to @@TUNNEL@@ forwarding 127.0.0.1:@@PORT@@ there to 127.0.0.1:22 here,
#     started again 30 seconds after it ends
#   - prints the fingerprint of this laptop's host key and the tunnel public key for the owner
#
# Everything it creates is named awb-winlab or lives in %ProgramData%\awb-winlab. A second run repeats the steps and
# keeps what is in place. `awb winlab remove @@MACHINE@@` prints remove.ps1, which removes all of it again.

$ErrorActionPreference = 'Stop'

$Machine = '@@MACHINE@@'
$Account = '@@ACCOUNT@@'
$Port = '@@PORT@@'
$HostKeyLine = '@@HOST_KEY@@'
$KnownHostLine = '@@KNOWN_HOST@@'
$Tunnel = '@@TUNNEL@@'
$TunnelUser = '@@TUNNEL_USER@@'
$TaskName = 'awb-winlab-tunnel'

$OpenSsh = Join-Path $env:WINDIR 'System32\OpenSSH'
$SshDir = Join-Path $env:ProgramData 'ssh'
$Dir = Join-Path $env:ProgramData 'awb-winlab'
$StateFile = Join-Path $Dir 'state.txt'
$SshdConfig = Join-Path $SshDir 'sshd_config'
$HostKey = Join-Path $SshDir 'ssh_host_ed25519_key'
$AuthorizedKeys = Join-Path $SshDir 'administrators_authorized_keys'
$TunnelKey = Join-Path $Dir 'tunnel_ed25519'
$KnownHosts = Join-Path $Dir 'known_hosts'
$TunnelScript = Join-Path $Dir 'tunnel.ps1'
$AdminsSid = 'S-1-5-32-544'
$HyperVAdminsSid = 'S-1-5-32-578'
$SystemSid = 'S-1-5-18'


# ------------------------------------------------------------------------------------------------- helpers

function Stop-Setup([string]$Step, [string]$Why) {
    $line = ($Why -replace '\s+', ' ').Trim()
    Write-Host ('awb-winlab setup: stopped at step {0}: {1}' -f $Step, $line)
    exit 1
}

# One step: $Do changes the laptop, $Check reads the result back and must end with $true. Anything else stops the
# script with one line naming the step.
function Step([string]$Name, [scriptblock]$Do, [scriptblock]$Check) {
    try { & $Do | Out-Null } catch { Stop-Setup $Name $_.Exception.Message }
    $result = $null
    try { $result = @(& $Check) } catch { Stop-Setup $Name $_.Exception.Message }
    if ($result.Count -eq 0 -or $result[-1] -ne $true) { Stop-Setup $Name 'its result check failed' }
    Write-Host ('awb-winlab setup: {0} ok' -f $Name)
}

# A program with a fixed command line: its standard output; on a failure an exception with its exit code and last
# error line.
function Invoke-Exe([string]$Exe, [string]$CommandLine) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $CommandLine
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $err = $errTask.Result
    if ($p.ExitCode -ne 0) {
        $last = @(($err + "`n" + $out).Split("`n") | Where-Object { $_.Trim() }) | Select-Object -Last 1
        throw ('{0} exited with {1}: {2}' -f (Split-Path $Exe -Leaf), $p.ExitCode, $last)
    }
    return $out
}

function Write-Ascii([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.ASCIIEncoding))
}

# Inheritance off, Administrators and SYSTEM full control, Administrators the owner.
function Set-AdminOnlyAcl([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inherit = 'ContainerInherit,ObjectInherit'
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inherit = 'None'
    }
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($AdminsSid, $SystemSid)) {
        $id = New-Object System.Security.Principal.SecurityIdentifier($sid)
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($id, 'FullControl', $inherit, 'None', 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier($AdminsSid)))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Test-AdminOnlyAcl([string]$Path) {
    $acl = Get-Acl -LiteralPath $Path
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $sids = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
              ForEach-Object { $_.IdentityReference.Value } | Sort-Object -Unique)
    return (($sids -join ',') -eq ($SystemSid + ',' + $AdminsSid))
}

# state.txt records what was on the laptop before the first run, so remove.ps1 takes away only what setup made.
function Get-State([string]$Key) {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $StateFile) {
        if ($line -like ($Key + '=*')) { return $line.Substring($Key.Length + 1) }
    }
    return $null
}

function Set-State([string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path -LiteralPath $StateFile) { $lines = @(Get-Content -LiteralPath $StateFile | Where-Object { $_ -notlike ($Key + '=*') }) }
    $lines += ($Key + '=' + $Value)
    Write-Ascii $StateFile (($lines -join "`r`n") + "`r`n")
}

function Get-SshdCapability {
    return @(Get-WindowsCapability -Online | Where-Object { $_.Name -like 'OpenSSH.Server*' }) | Select-Object -First 1
}

function Get-LabUser {
    return Get-LocalUser -Name $Account -ErrorAction SilentlyContinue
}

# Through ADSI: Get-LocalGroupMember fails on a group that holds an account it cannot resolve.
function Get-GroupAdsi([string]$GroupSid) {
    $group = Get-LocalGroup -SID $GroupSid -ErrorAction SilentlyContinue
    if (-not $group) { throw ('the group {0} is missing: turn on Hyper-V first' -f $GroupSid) }
    return [ADSI]('WinNT://{0}/{1},group' -f $env:COMPUTERNAME, $group.Name)
}

function Test-GroupMember([string]$GroupSid, [string]$UserSid) {
    foreach ($m in @((Get-GroupAdsi $GroupSid).psbase.Invoke('Members'))) {
        $bytes = $m.GetType().InvokeMember('objectSid', 'GetProperty', $null, $m, $null)
        if ((New-Object System.Security.Principal.SecurityIdentifier($bytes, 0)).Value -eq $UserSid) { return $true }
    }
    return $false
}

# Every listening socket of the sshd process is on 127.0.0.1 and one of them is port 22.
function Test-SshdLoopbackOnly {
    $svc = Get-CimInstance -ClassName Win32_Service -Filter "Name='sshd'"
    if (-not $svc -or $svc.State -ne 'Running' -or -not $svc.ProcessId) { return $false }
    $listen = @(Get-NetTCPConnection -State Listen -OwningProcess $svc.ProcessId -ErrorAction SilentlyContinue)
    if ($listen.Count -eq 0) { return $false }
    $other = @($listen | Where-Object { $_.LocalAddress -ne '127.0.0.1' })
    $ssh = @($listen | Where-Object { $_.LocalPort -eq 22 })
    return ($other.Count -eq 0 -and $ssh.Count -ge 1)
}


# ------------------------------------------------------------------------------------------------- the steps

function Test-Administrator {
    $principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
    return $principal.IsInRole([System.Security.Principal.SecurityIdentifier]$AdminsSid)
}

Step 'administrator' {
    if (-not (Test-Administrator)) { throw 'run it in a PowerShell window opened as administrator' }
} {
    Test-Administrator
}

Step 'state-folder' {
    if (-not (Test-Path -LiteralPath $Dir)) { New-Item -ItemType Directory -Path $Dir | Out-Null }
    Set-AdminOnlyAcl $Dir
    $before = Get-State 'machine'
    if ($before -and ($before -ne $Machine -or (Get-State 'account') -ne $Account)) {
        throw ('this laptop is set up as lab machine {0} with account {1}: run its remove.ps1 first' -f $before, (Get-State 'account'))
    }
    if ($null -eq $before) {
        $cap = Get-SshdCapability
        $installed = ($cap -and $cap.State -eq 'Installed')
        $user = Get-LabUser
        if ($user) { throw ('a local account {0} exists already and this script did not make it: choose another with awb winlab bootstrap --account' -f $Account) }
        $startup = 'none'
        if ($installed) { $startup = [string](Get-Service -Name sshd).StartType }
        Set-State 'openssh_before' ([string]$installed).ToLower()
        Set-State 'sshd_start_before' $startup
        Set-State 'host_key_before' ([string](Test-Path -LiteralPath $HostKey)).ToLower()
        if (Test-Path -LiteralPath $SshdConfig) { Copy-Item -LiteralPath $SshdConfig -Destination (Join-Path $Dir 'sshd_config.before') -Force }
        if (Test-Path -LiteralPath $AuthorizedKeys) { Copy-Item -LiteralPath $AuthorizedKeys -Destination (Join-Path $Dir 'administrators_authorized_keys.before') -Force }
        Set-State 'account' $Account
        Set-State 'machine' $Machine
    }
} {
    (Test-AdminOnlyAcl $Dir) -and (Get-State 'machine') -eq $Machine -and (Get-State 'account') -eq $Account
}

Step 'openssh-server' {
    $cap = Get-SshdCapability
    if (-not $cap) { throw 'Windows offers no OpenSSH.Server capability on this laptop' }
    if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
} {
    $cap = Get-SshdCapability
    $cap -and $cap.State -eq 'Installed' -and (Test-Path -LiteralPath (Join-Path $OpenSsh 'sshd.exe')) -and
        [bool](Get-Service -Name sshd -ErrorAction SilentlyContinue)
}

# A fresh sshd listens on every address: it stays stopped until the new configuration is in place.
Step 'sshd-stop' {
    Stop-Service -Name sshd -Force
    Set-Service -Name sshd -StartupType Manual
} {
    (Get-Service -Name sshd).Status -eq 'Stopped'
}

Step 'host-key' {
    if (-not (Test-Path -LiteralPath $SshDir)) { New-Item -ItemType Directory -Path $SshDir | Out-Null }
    if (-not (Test-Path -LiteralPath $HostKey)) {
        Invoke-Exe (Join-Path $OpenSsh 'ssh-keygen.exe') ('-q -t ed25519 -N "" -C awb-winlab-host-key -f "{0}"' -f $HostKey) | Out-Null
    }
    Set-AdminOnlyAcl $HostKey
} {
    (Test-Path -LiteralPath $HostKey) -and (Test-Path -LiteralPath ($HostKey + '.pub')) -and (Test-AdminOnlyAcl $HostKey)
}

# The password is random, held in memory as a SecureString only and never shown: the account logs on with a key.
Step 'account' {
    if (-not (Get-LabUser)) {
        $bytes = New-Object byte[] 64
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $secret = New-Object System.Security.SecureString
        foreach ($b in $bytes[0..60]) { $secret.AppendChar([char](33 + ($b % 94))) }
        $secret.AppendChar([char](97 + ($bytes[61] % 26)))
        $secret.AppendChar([char](65 + ($bytes[62] % 26)))
        $secret.AppendChar([char](48 + ($bytes[63] % 10)))
        New-LocalUser -Name $Account -Password $secret -FullName 'awb-winlab' -Description 'awb-winlab: host logs on by key only' -PasswordNeverExpires -AccountNeverExpires | Out-Null
        $secret.Dispose()
    }
    Set-State 'account_sid' (Get-LabUser).SID.Value
} {
    $user = Get-LabUser
    $user -and $user.Enabled -and (Get-State 'account_sid') -eq $user.SID.Value
}

Step 'groups' {
    $sid = (Get-LabUser).SID.Value
    foreach ($group in @($AdminsSid, $HyperVAdminsSid)) {
        if (-not (Test-GroupMember $group $sid)) { (Get-GroupAdsi $group).Add(('WinNT://{0}/{1},user' -f $env:COMPUTERNAME, $Account)) }
    }
} {
    $sid = (Get-LabUser).SID.Value
    (Test-GroupMember $AdminsSid $sid) -and (Test-GroupMember $HyperVAdminsSid $sid)
}

Step 'authorized-keys' {
    Write-Ascii $AuthorizedKeys ($HostKeyLine + "`r`n")
    Set-AdminOnlyAcl $AuthorizedKeys
} {
    ((Get-Content -LiteralPath $AuthorizedKeys) -join '') -eq $HostKeyLine -and (Test-AdminOnlyAcl $AuthorizedKeys)
}

Step 'sshd-config' {
    $config = @(
        ('# awb-winlab: written by setup.ps1 for lab machine ' + $Machine + '; remove.ps1 puts back the file from before'),
        'Port 22',
        'AddressFamily inet',
        'ListenAddress 127.0.0.1',
        'HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key',
        'PubkeyAuthentication yes',
        'AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys',
        'AuthenticationMethods publickey',
        'PasswordAuthentication no',
        'PermitEmptyPasswords no',
        ('AllowUsers ' + $Account),
        'AllowTcpForwarding no',
        'GatewayPorts no',
        'PermitTunnel no',
        'Subsystem sftp sftp-server.exe'
    )
    Write-Ascii $SshdConfig (($config -join "`r`n") + "`r`n")
    Invoke-Exe (Join-Path $OpenSsh 'sshd.exe') ('-t -f "{0}"' -f $SshdConfig) | Out-Null
} {
    $text = Get-Content -LiteralPath $SshdConfig
    @($text | Where-Object { $_ -like 'ListenAddress *' }).Count -eq 1 -and ($text -contains 'ListenAddress 127.0.0.1') -and
        ($text -contains 'PasswordAuthentication no')
}

Step 'sshd-start' {
    Set-Service -Name sshd -StartupType Automatic
    Start-Service -Name sshd
    for ($i = 0; $i -lt 20 -and -not (Test-SshdLoopbackOnly); $i++) { Start-Sleep -Milliseconds 500 }
} {
    (Get-Service -Name sshd).StartType -eq 'Automatic' -and (Test-SshdLoopbackOnly)
}

Step 'tunnel-key' {
    if (-not (Test-Path -LiteralPath $TunnelKey)) {
        Invoke-Exe (Join-Path $OpenSsh 'ssh-keygen.exe') ('-q -t ed25519 -N "" -C awb-winlab-tunnel-{0} -f "{1}"' -f $Machine, $TunnelKey) | Out-Null
    }
    Set-AdminOnlyAcl $TunnelKey
} {
    (Test-Path -LiteralPath $TunnelKey) -and (Test-Path -LiteralPath ($TunnelKey + '.pub')) -and (Test-AdminOnlyAcl $TunnelKey)
}

Step 'known-host' {
    Write-Ascii $KnownHosts ($KnownHostLine + "`r`n")
    Set-AdminOnlyAcl $KnownHosts
} {
    ((Get-Content -LiteralPath $KnownHosts) -join '') -eq $KnownHostLine
}

Step 'tunnel-script' {
    $script = @'
# awb-winlab-tunnel: written by setup.ps1 of the Architect Workbench for lab machine @@MACHINE@@, started at boot by
# the scheduled task awb-winlab-tunnel. It holds one outgoing SSH connection to @@TUNNEL@@ that forwards
# 127.0.0.1:@@PORT@@ there to 127.0.0.1:22 here and starts it again 30 seconds after it ends.
$dir = Join-Path $env:ProgramData 'awb-winlab'
$log = Join-Path $dir 'tunnel.log'
$ssh = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
$arguments = @('-N', '-F', 'none', '-i', (Join-Path $dir 'tunnel_ed25519'), '-o', 'IdentitiesOnly=yes',
               '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
               '-o', ('UserKnownHostsFile=' + (Join-Path $dir 'known_hosts')),
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', '-o', 'ExitOnForwardFailure=yes',
               '-p', '@@SSH_PORT@@', '-R', '127.0.0.1:@@PORT@@:127.0.0.1:22', '@@TUNNEL@@')
while ($true) {
    $start = Get-Date
    $out = @(& $ssh @arguments 2>&1 | ForEach-Object { "$_" })
    $code = $LASTEXITCODE
    $last = ($out | Select-Object -Last 1)
    if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 1MB) { Move-Item -LiteralPath $log -Destination ($log + '.1') -Force }
    Add-Content -LiteralPath $log -Value ('{0} ssh ended with {1} after {2} s, again in 30 s: {3}' -f (Get-Date -Format s), $code, [int]((Get-Date) - $start).TotalSeconds, $last)
    Start-Sleep -Seconds 30
}
'@
    Write-Ascii $TunnelScript $script
    Set-AdminOnlyAcl $TunnelScript
} {
    $text = (Get-Content -LiteralPath $TunnelScript) -join "`n"
    $text.Contains('ExitOnForwardFailure=yes') -and $text.Contains('127.0.0.1:' + $Port + ':127.0.0.1:22')
}

Step 'tunnel-task' {
    $ps = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $ps -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $TunnelScript)
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
        -Description ('awb-winlab: the SSH tunnel of lab machine {0} to the Workbench host, {1}' -f $Machine, $TunnelScript) -Force | Out-Null
} {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\'
    $boot = @($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' })
    $task -and -not $task.Settings.Hidden -and $boot.Count -eq 1 -and $task.Principal.UserId -like '*SYSTEM'
}

Step 'tunnel-start' {
    if ((Get-ScheduledTask -TaskName $TaskName -TaskPath '\').State -ne 'Running') { Start-ScheduledTask -TaskName $TaskName -TaskPath '\' }
    Start-Sleep -Seconds 3
} {
    (Get-ScheduledTask -TaskName $TaskName -TaskPath '\').State -eq 'Running'
}

$fingerprint = $null
$tunnelPub = $null
Step 'report' {
    $line = Invoke-Exe (Join-Path $OpenSsh 'ssh-keygen.exe') ('-l -E sha256 -f "{0}.pub"' -f $HostKey)
    $script:fingerprint = @($line.Trim().Split(' ') | Where-Object { $_ -like 'SHA256:*' }) | Select-Object -First 1
    $script:tunnelPub = ((Get-Content -LiteralPath ($TunnelKey + '.pub')) -join '').Trim()
} {
    $script:fingerprint -match '^SHA256:[A-Za-z0-9+/]{43}$' -and $script:tunnelPub -like 'ssh-ed25519 *'
}

Write-Host ''
Write-Host ('awb-winlab setup: done for lab machine {0}' -f $Machine)
Write-Host ('host key fingerprint of this laptop: {0}' -f $fingerprint)
Write-Host ('tunnel public key: {0}' -f $tunnelPub)
Write-Host ''
Write-Host 'On the Workbench host:'
Write-Host ('  awb winlab add {0} --port {1} --fingerprint {2} --no-customer-data' -f $Machine, $Port, $fingerprint)
Write-Host ('  and this line in the authorized_keys of {0}, so the tunnel may forward its one port and nothing else:' -f $TunnelUser)
Write-Host ('  restrict,port-forwarding,permitlisten="127.0.0.1:{0}" {1}' -f $Port, $tunnelPub)
Write-Host ('The tunnel tries every 30 seconds until the host takes the key; its log: {0}\tunnel.log' -f $Dir)
