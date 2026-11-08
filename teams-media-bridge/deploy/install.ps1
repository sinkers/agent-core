<#
.SYNOPSIS
    install.ps1 - Install or upgrade the Teams media bridge on its Windows Server VM.

.DESCRIPTION
    Idempotent. Run ON the VM as Administrator/SYSTEM, typically via Azure Run Command, e.g.

      az vm run-command create -g <rg> --vm-name <vm> --name install-bridge --async-execution false `
        --script @deploy/install.ps1 `
        --parameters PackageUrl=<sas-url> Fqdn=<fqdn> Email=<email> AppId=<guid> TenantId=<guid> `
                     AgentSessionUrl=<url> `
        --protected-parameters AppSecret=<secret> AgentBridgeToken=<token>

    (Use --protected-parameters for secrets so they are not shown in the run-command instance view.)

    Steps (each skipped when already done):
      1. .NET 8 runtime (Microsoft.NETCore.App + Microsoft.AspNetCore.App) into C:\Program Files\dotnet
      2. Visual C++ 2015-2022 x64 redistributable and the Server-Media-Foundation feature
         (media platform prerequisites; see the ElevenLabs Graph media bot guide, "Windows VM Setup")
      3. C:\teams-media-bridge\{app,config,logs,win-acme}; stop the service; unpack the release zip into app\
      4. Windows Firewall: TCP 80 (ACME HTTP-01), signalling port (443), media port (8445)
      5. win-acme + Let's Encrypt certificate for -Fqdn via HTTP-01 self-hosting on port 80, into
         LocalMachine\My; renewals run update-cert.ps1 which rewrites the thumbprint and restarts the service when idle
      6. config\bridge.json (secrets; ACL SYSTEM + Administrators only). Secrets are never printed.
      7. Windows service "TeamsMediaBridge" (LocalSystem, automatic, restart on failure) and start it
      8. GET /healthz

    A reboot may be required after the Server-Media-Foundation feature is first installed; the script says so.

.PARAMETER PackageUrl
    HTTPS (SAS) URL of teams-media-bridge.zip produced by deploy/publish.sh.
.PARAMETER Fqdn
    Public DNS name of this VM, e.g. voicebot.example.com.
.PARAMETER Email
    Contact e-mail for the Let's Encrypt account.
.PARAMETER CertificateThumbprint
    Optional: use this existing LocalMachine\My certificate and skip win-acme.
.EXAMPLE
    .\install.ps1 -PackageUrl 'https://...zip?sv=...' -Fqdn voicebot.example.com `
        -Email ops@example.com -AppId ... -AppSecret ... -TenantId ... -AgentSessionUrl ... -AgentBridgeToken ...
.EXAMPLE
    .\install.ps1 -Help
#>
[CmdletBinding()]
param(
    [string]$PackageUrl,
    [string]$Fqdn,
    [string]$Email,
    [string]$AppId,
    [string]$AppSecret,
    [string]$TenantId,
    [string]$AgentSessionUrl,
    [string]$AgentBridgeToken,
    [string]$CertificateThumbprint = '',
    [int]$SignalingPort = 443,
    [int]$MediaPort = 8445,
    [int]$MaxConcurrentCalls = 4,
    [string]$InstallRoot = 'C:\teams-media-bridge',
    [string]$ServiceName = 'TeamsMediaBridge',
    [string]$WinAcmeZipUrl = '',
    [switch]$Help
)

Set-StrictMode -Version Latest
# Windows PowerShell 5.1 defaults to TLS 1.0; Kestrel and GitHub need TLS 1.2.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if ($Help) { Get-Help $MyInvocation.MyCommand.Path -Detailed; exit 0 }

function Write-Step([string]$Message) { Write-Output "==> $Message" }
function Write-Info([string]$Message) { Write-Output "    $Message" }

# ---------------------------------------------------------------- validation
$missing = @()
foreach ($name in 'PackageUrl', 'Fqdn', 'AppId', 'AppSecret', 'TenantId', 'AgentSessionUrl', 'AgentBridgeToken') {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) { $missing += $name }
}
if ([string]::IsNullOrWhiteSpace($CertificateThumbprint) -and [string]::IsNullOrWhiteSpace($Email)) { $missing += 'Email' }
if ($missing.Count -gt 0) { throw "Missing required parameter(s): $($missing -join ', '). Run with -Help." }
if ($PackageUrl -notmatch '^https://') { throw 'PackageUrl must be https://' }
if ($AgentSessionUrl -notmatch '^https://') { throw 'AgentSessionUrl must be https://' }
if ($AgentBridgeToken.Length -lt 16) { throw 'AgentBridgeToken must be at least 16 characters' }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as Administrator (or SYSTEM via Run Command).' }

$AppDir = Join-Path $InstallRoot 'app'
$ConfigDir = Join-Path $InstallRoot 'config'
$LogDir = Join-Path $InstallRoot 'logs'
$AcmeDir = Join-Path $InstallRoot 'win-acme'
$ConfigFile = Join-Path $ConfigDir 'bridge.json'
$DotnetDir = Join-Path $env:ProgramFiles 'dotnet'
$RebootNeeded = $false

# ---------------------------------------------------------------- 1. .NET 8 runtime
function Test-Runtime([string]$Framework) {
    $exe = Join-Path $DotnetDir 'dotnet.exe'
    if (-not (Test-Path $exe)) { return $false }
    $list = & $exe --list-runtimes 2>$null
    return [bool]($list | Where-Object { $_ -match "^$([regex]::Escape($Framework)) 8\." })
}

Write-Step '.NET 8 runtime'
if ((Test-Runtime 'Microsoft.NETCore.App') -and (Test-Runtime 'Microsoft.AspNetCore.App')) {
    Write-Info 'already installed'
} else {
    # Official installer script: https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-install-script
    $installer = Join-Path $env:TEMP 'dotnet-install.ps1'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installer
    & $installer -Channel 8.0 -Runtime dotnet -Architecture x64 -InstallDir $DotnetDir -NoPath
    & $installer -Channel 8.0 -Runtime aspnetcore -Architecture x64 -InstallDir $DotnetDir -NoPath
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ($machinePath -notlike "*$DotnetDir*") { [Environment]::SetEnvironmentVariable('Path', "$machinePath;$DotnetDir", 'Machine') }
    if (-not (Test-Runtime 'Microsoft.AspNetCore.App')) { throw '.NET 8 runtime install failed' }
    Write-Info "installed to $DotnetDir"
}

# ---------------------------------------------------------------- 2. media platform prerequisites
Write-Step 'Visual C++ 2015-2022 x64 redistributable'
$vc = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
if ($vc -and $vc.Installed -eq 1) {
    Write-Info "already installed ($($vc.Version))"
} else {
    $vcExe = Join-Path $env:TEMP 'vc_redist.x64.exe'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $vcExe
    $p = Start-Process -FilePath $vcExe -ArgumentList '/install', '/quiet', '/norestart' -Wait -PassThru
    if ($p.ExitCode -eq 3010) { $RebootNeeded = $true } elseif ($p.ExitCode -ne 0) { throw "vc_redist failed ($($p.ExitCode))" }
    Write-Info 'installed'
}

Write-Step 'Server-Media-Foundation feature'
$feature = Get-WindowsFeature -Name Server-Media-Foundation
if ($feature.Installed) {
    Write-Info 'already installed'
} else {
    $r = Install-WindowsFeature -Name Server-Media-Foundation
    if ($r.RestartNeeded -eq 'Yes') { $RebootNeeded = $true }
    Write-Info 'installed'
}

# ---------------------------------------------------------------- 3. folders + package
Write-Step "Folders under $InstallRoot"
foreach ($d in $InstallRoot, $AppDir, $ConfigDir, $LogDir, $AcmeDir) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -ne 'Stopped') {
    Write-Step "Stopping $ServiceName (active calls will drop)"
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
}
# The service can report Stopped while its process (media platform) still holds the DLLs.
function Get-AppProcesses { @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($AppDir, [StringComparison]::OrdinalIgnoreCase) }) }
for ($i = 0; $i -lt 15 -and @(Get-AppProcesses).Count -gt 0; $i++) { Start-Sleep -Seconds 2 }
Get-AppProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

Write-Step 'Downloading and extracting release package'
$zip = Join-Path $env:TEMP 'teams-media-bridge.zip'
try {
    Invoke-WebRequest -UseBasicParsing -Uri $PackageUrl -OutFile $zip
} catch {
    # Do not let the exception text echo the SAS URL.
    throw "Package download failed (HTTP status: $($_.Exception.Response.StatusCode)). Check the SAS URL and its expiry."
}
Get-ChildItem -Path $AppDir -Force | Remove-Item -Recurse -Force
Expand-Archive -Path $zip -DestinationPath $AppDir -Force
Remove-Item $zip -Force
if (-not (Test-Path (Join-Path $AppDir 'TeamsMediaBridge.exe'))) { throw 'Package does not contain TeamsMediaBridge.exe at its root' }
Write-Info "extracted to $AppDir ($((Get-FileHash (Join-Path $AppDir 'TeamsMediaBridge.dll')).Hash.Substring(0,12)))"

# Optional: media platform performance counters. The SDK's script needs PowerShell 7 (pwsh) because it
# imports a .NET 8 module; skip with a note if pwsh is absent (counters are diagnostics only).
if (Get-Command pwsh.exe -ErrorAction SilentlyContinue) {
    Push-Location $AppDir
    try { cmd.exe /c MediaPlatformStartupScript.bat | Out-Null; Write-Info 'media perf counters registered' }
    catch { Write-Info "media perf counters not registered: $($_.Exception.Message)" }
    finally { Pop-Location }
} else {
    Write-Info 'pwsh not installed: skipping optional media perf counter registration'
}

# ---------------------------------------------------------------- 4. firewall
Write-Step 'Windows Firewall'
$rules = @(
    @{ Name = 'TeamsMediaBridge-ACME-HTTP'; Port = 80;             Desc = "Let's Encrypt HTTP-01 validation (win-acme self-hosting)" },
    @{ Name = 'TeamsMediaBridge-Signaling'; Port = $SignalingPort; Desc = 'Graph call notifications (HTTPS)' },
    @{ Name = 'TeamsMediaBridge-Media';     Port = $MediaPort;     Desc = 'Teams real-time media (TCP)' }
)
foreach ($rule in $rules) {
    $existing = Get-NetFirewallRule -Name $rule.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Set-NetFirewallRule -Name $rule.Name -Enabled True -Action Allow -Direction Inbound
        Get-NetFirewallRule -Name $rule.Name | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $rule.Port
    } else {
        New-NetFirewallRule -Name $rule.Name -DisplayName $rule.Name -Description $rule.Desc -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $rule.Port -Profile Any | Out-Null
    }
    Write-Info "TCP $($rule.Port) open ($($rule.Name))"
}

# ---------------------------------------------------------------- 5. certificate (win-acme)
# Renewal hook: win-acme runs this with the new thumbprint ({CertThumbprint} placeholder,
# https://www.win-acme.com/reference/plugins/installation/script). It updates bridge.json and restarts the
# service once no calls are active (waits up to 2 h; the old cert stays valid ~30 days after renewal).
$UpdateCertScript = Join-Path $InstallRoot 'update-cert.ps1'
@"
param([Parameter(Mandatory=`$true)][string]`$Thumbprint)
`$ErrorActionPreference = 'Stop'
`$config = '$ConfigFile'
`$json = Get-Content `$config -Raw | ConvertFrom-Json
if (`$json.Bot.CertificateThumbprint -eq `$Thumbprint) { exit 0 }
`$json.Bot.CertificateThumbprint = `$Thumbprint
`$json | ConvertTo-Json -Depth 10 | Set-Content -Path `$config -Encoding UTF8
# First issuance happens before the service exists/is configured: just record the thumbprint.
`$svc = Get-Service -Name '$ServiceName' -ErrorAction SilentlyContinue
if (-not `$svc -or `$svc.Status -ne 'Running') { exit 0 }
`$deadline = (Get-Date).AddHours(2)
while ((Get-Date) -lt `$deadline) {
    # -k: the running service still presents the old certificate; this is a local probe only.
    `$body = & curl.exe -sk -m 5 'https://127.0.0.1:$SignalingPort/healthz' 2>`$null
    if (`$LASTEXITCODE -ne 0 -or `$body -match '"activeCalls"\s*:\s*0\b') { break }
    Start-Sleep -Seconds 60
}
Restart-Service -Name '$ServiceName' -Force
"@ | Set-Content -Path $UpdateCertScript -Encoding UTF8

function Find-FqdnCertificate {
    Get-ChildItem Cert:\LocalMachine\My |
        Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date).AddDays(14) -and
                       # Some certs have no DNS names; under StrictMode $null.Unicode would throw.
                       (@($_.DnsNameList) | Where-Object { $_ } | ForEach-Object { $_.Unicode }) -contains $Fqdn } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
}

Write-Step "Certificate for $Fqdn"
if (-not [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    $CertificateThumbprint = $CertificateThumbprint -replace '\s', ''
    if (-not (Test-Path "Cert:\LocalMachine\My\$CertificateThumbprint")) { throw "Certificate $CertificateThumbprint not in LocalMachine\My" }
    Write-Info "using supplied certificate $CertificateThumbprint"
} else {
    $wacs = Join-Path $AcmeDir 'wacs.exe'
    if (-not (Test-Path $wacs)) {
        if ([string]::IsNullOrWhiteSpace($WinAcmeZipUrl)) {
            # Latest release, "trimmed" x64 build (recommended for most users per win-acme docs).
            $release = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/win-acme/win-acme/releases/latest' -Headers @{ 'User-Agent' = 'teams-media-bridge-install' }
            $asset = $release.assets | Where-Object { $_.name -match '\.x64\.trimmed\.zip$' } | Select-Object -First 1
            if (-not $asset) { throw 'Could not find a win-acme x64 trimmed release asset' }
            $WinAcmeZipUrl = $asset.browser_download_url
        }
        $acmeZip = Join-Path $env:TEMP 'win-acme.zip'
        Invoke-WebRequest -UseBasicParsing -Uri $WinAcmeZipUrl -OutFile $acmeZip
        Expand-Archive -Path $acmeZip -DestinationPath $AcmeDir -Force
        Remove-Item $acmeZip -Force
        Write-Info "win-acme installed to $AcmeDir"
    }

    $cert = Find-FqdnCertificate
    if ($cert) {
        # Avoid re-issuing on every run (Let's Encrypt duplicate-certificate rate limits).
        Write-Info "existing certificate $($cert.Thumbprint) valid until $($cert.NotAfter.ToString('u'))"
    } else {
        # Unattended: https://www.win-acme.com/reference/cli . Self-hosting validation listens on port 80
        # itself, so nothing else may hold port 80 (the bridge only listens on $SignalingPort).
        $acmeArgs = @(
            '--source', 'manual', '--host', $Fqdn,
            '--validation', 'selfhosting', '--validationport', '80',
            '--store', 'certificatestore', '--certificatestore', 'My',
            '--installation', 'script', '--script', $UpdateCertScript, '--scriptparameters', "'{CertThumbprint}'",
            '--emailaddress', $Email, '--accepttos', '--closeonfinish'
        )
        # bridge.json must exist before win-acme calls update-cert.ps1 (it edits it); write a stub if needed.
        if (-not (Test-Path $ConfigFile)) { '{"Bot":{"CertificateThumbprint":""}}' | Set-Content -Path $ConfigFile -Encoding UTF8 }
        & $wacs @acmeArgs
        if ($LASTEXITCODE -ne 0) { throw "win-acme failed (exit $LASTEXITCODE). Check DNS for $Fqdn and that TCP 80 is open in the NSG." }
        $cert = Find-FqdnCertificate
        if (-not $cert) { throw "win-acme finished but no certificate for $Fqdn is in LocalMachine\My" }
        Write-Info "issued certificate $($cert.Thumbprint) valid until $($cert.NotAfter.ToString('u'))"
    }
    $CertificateThumbprint = $cert.Thumbprint
}

# ---------------------------------------------------------------- 6. config (secrets)
Write-Step "Writing $ConfigFile (values not shown)"
$config = [ordered]@{
    Bot    = [ordered]@{
        AppId                 = $AppId
        AppSecret             = $AppSecret
        TenantId              = $TenantId
        ServiceFqdn           = $Fqdn
        CertificateThumbprint = $CertificateThumbprint
        InstancePublicPort    = $MediaPort
        InstanceInternalPort  = $MediaPort
        CallSignalingPort     = $SignalingPort
        MaxConcurrentCalls    = $MaxConcurrentCalls
    }
    Agent = [ordered]@{
        SessionUrl  = $AgentSessionUrl
        BridgeToken = $AgentBridgeToken
    }
    Logs   = [ordered]@{ Directory = $LogDir }
}
$config | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigFile -Encoding UTF8
# Only SYSTEM (the service account) and Administrators may read it.
& icacls.exe $ConfigDir /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' | Out-Null
& icacls.exe $ConfigFile /inheritance:r /grant:r 'SYSTEM:F' 'Administrators:F' | Out-Null

# ---------------------------------------------------------------- 7. service
Write-Step "Windows service $ServiceName"
$exe = Join-Path $AppDir 'TeamsMediaBridge.exe'
if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
    New-Service -Name $ServiceName -BinaryPathName "`"$exe`"" -DisplayName 'Teams Media Bridge (agent voice)' `
        -Description 'Relays Teams real-time audio to the agent's ElevenLabs conversation.' -StartupType Automatic | Out-Null
    Write-Info 'created'
} else {
    & sc.exe config $ServiceName binPath= "`"$exe`"" start= auto | Out-Null
}
# Restart after 5 s, 5 s, then 30 s; reset the failure count after a day. failureflag=1 also applies the
# actions when the process exits with an error code rather than crashing.
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
& sc.exe failureflag $ServiceName 1 | Out-Null
# Non-secret environment only; secrets live in bridge.json.
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName" -Name Environment -Type MultiString `
    -Value @("BRIDGE_CONFIG=$ConfigFile", 'DOTNET_ENVIRONMENT=Production', "DOTNET_ROOT=$DotnetDir")

if ($RebootNeeded) {
    Write-Output ''
    Write-Output 'REBOOT REQUIRED: a prerequisite needs a restart. The service is set to Automatic and will start after reboot.'
    Write-Output 'Run: Restart-Computer -Force   (then re-run this script or check /healthz)'
    exit 0
}

Start-Service -Name $ServiceName
Write-Info 'started'

# ---------------------------------------------------------------- 8. health
Write-Step 'Health check'
# PowerShell's cert callback can't run under the Run Command host (no runspace), so use curl.exe,
# pinned to 127.0.0.1 so the real certificate for the FQDN is still validated.
$healthy = $false
for ($i = 0; $i -lt 30 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    $body = & curl.exe -s -m 5 --resolve "${Fqdn}:${SignalingPort}:127.0.0.1" "https://${Fqdn}:${SignalingPort}/healthz" 2>$null
    if ($LASTEXITCODE -eq 0 -and $body -match '"status"\s*:\s*"ok"') {
        Write-Info "healthz: $body"
        $healthy = $true
    } elseif ((Get-Service -Name $ServiceName).Status -ne 'Running') { break }
}
if (-not $healthy) {
    Write-Output "Health check FAILED. Last log lines from $LogDir :"
    Get-ChildItem $LogDir -Filter 'bridge-*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1 |
        ForEach-Object { Get-Content $_.FullName -Tail 40 }
    exit 1
}
Write-Output "OK: https://$Fqdn/healthz  (Graph calling webhook: https://$Fqdn/api/calling)"
