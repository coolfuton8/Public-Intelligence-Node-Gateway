# MCPing: connect Claude Code on this computer to the device at @@ADDRESS@@
#
#   powershell -ExecutionPolicy Bypass -File .\setup-claude-code.ps1
#
# It saves the device certificate authority (CA), tells Claude Code to trust it and registers
# the MCP server. It asks for the device token (on the device: MCPing Connect > Copy token only).
param(
    [string]$Token = $env:MCPING_TOKEN,
    [string]$Name = 'mcping',
    [string]$Address = '@@ADDRESS@@'
)
$ErrorActionPreference = 'Stop'

$url = "https://${Address}:@@PORT@@/mcp"
$caPem = @'
@@CA_PEM@@
'@
$expected = '@@FINGERPRINT@@'

# Find the claude command: on PATH, or in the places the installers put it.
$claude = (Get-Command claude -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $claude) {
    foreach ($p in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:USERPROFILE\.claude\local\claude.exe",
                     "$env:APPDATA\npm\claude.cmd", "$env:LOCALAPPDATA\Programs\claude\claude.exe")) {
        if (Test-Path $p) { $claude = $p; break }
    }
}
if (-not $Token) { $Token = '@@TOKEN@@' }   # filled in only for a script obtained with a one-time invite
if (-not $Token) {
    $sec = Read-Host 'Device token (on the device: MCPing Connect > Copy token only)' -AsSecureString
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if ($Token.Length -lt 24) { throw 'That does not look like a device token (too short).' }

# 1. the CA file, checked against the fingerprint this script was generated with
$der = [Convert]::FromBase64String((($caPem -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^-----' }) -join ''))
$got = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($der)) -replace '-', '').ToLower()
if ($got -ne $expected) { throw "Certificate fingerprint mismatch ($got). Download the script again." }
$dir = Join-Path $env:USERPROFILE '.mcping'
New-Item -ItemType Directory -Force $dir | Out-Null
$ca = Join-Path $dir 'mcping-ca.crt'
[IO.File]::WriteAllText($ca, ($caPem.Trim() + "`n"), (New-Object Text.ASCIIEncoding))
Write-Host "Device CA saved to $ca"
Write-Host "  SHA-256 fingerprint: $got"
Write-Host '  Compare it with the one shown on the device (MCPing Certificate app) if this network is not yours.'

# 2. trust it for Claude Code (Node)
[Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $ca, 'User')
$env:NODE_EXTRA_CA_CERTS = $ca

# 2b. the Claude desktop app, if it is installed: its connectors list only loads local (stdio) servers, so a
# small bridge script (Windows PowerShell and .NET only, nothing to install) is added to the app's own config.
$desktopDone = $false
$appCfgDir = Join-Path $env:APPDATA 'Claude'
if (Test-Path $appCfgDir) {
    try {
        $bridge = Join-Path $dir 'mcping-bridge.ps1'
        [IO.File]::WriteAllBytes($bridge, [Convert]::FromBase64String('@@BRIDGE_PS_B64@@'))
        $cfgPath = Join-Path $appCfgDir 'claude_desktop_config.json'
        $cfg = $null
        if (Test-Path $cfgPath) {
            Copy-Item $cfgPath "$cfgPath.mcping-backup" -Force
            $cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        if (-not $cfg) { $cfg = [pscustomobject]@{} }
        if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers') -or -not $cfg.mcpServers) {
            $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $entry = [pscustomobject]@{
            command = 'powershell.exe'
            args    = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $bridge)
            env     = [pscustomobject]@{ MCPING_URL = $url; MCPING_TOKEN = $Token; MCPING_CA = $ca }
        }
        $cfg.mcpServers | Add-Member -NotePropertyName $Name -NotePropertyValue $entry -Force
        [IO.File]::WriteAllText($cfgPath, (ConvertTo-Json -InputObject $cfg -Depth 100), (New-Object Text.UTF8Encoding($false)))
        $desktopDone = $true
    } catch {
        Write-Host "Could not set up the Claude desktop app's connectors list ($($_.Exception.Message))."
    }
}
function Show-DesktopNote {
    if (-not $desktopDone) { return }
    Write-Host ''
    Write-Host 'The Claude desktop app was set up too (it will list "mcping" under its connectors).'
    Write-Host 'IMPORTANT: quit the Claude app completely (also its icon near the clock) and open it again, or it'
    Write-Host 'will not see mcping.'
}

# 3. register the server (cmd /c keeps PowerShell from mangling the arguments)
function Add-ClaudeUserServer([string]$Path, [string]$ServerName, [string]$ServerUrl, [string]$ServerToken) {
    # The desktop app's Code tab reads the same user settings file as the command-line tool
    # (~/.claude.json, "mcpServers"), so the entry the CLI would write is added directly.
    $entry = [pscustomobject]@{ type = 'http'; url = $ServerUrl; headers = [pscustomobject]@{ Authorization = "Bearer $ServerToken" } }
    $cfg = $null
    if (Test-Path $Path) {
        Copy-Item $Path "$Path.mcping-backup" -Force
        $cfg = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if (-not $cfg) { $cfg = [pscustomobject]@{} }
    if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers') -or -not $cfg.mcpServers) {
        $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    $cfg.mcpServers | Add-Member -NotePropertyName $ServerName -NotePropertyValue $entry -Force
    $text = ConvertTo-Json -InputObject $cfg -Depth 100
    [IO.File]::WriteAllText($Path, $text, (New-Object Text.UTF8Encoding($false)))
}
if (-not $claude) {
    # No command-line Claude Code here (typically only the desktop app): write the entry into the user
    # settings file the app reads, and keep a ready-made .mcp.json as a fallback.
    $json = '{"mcpServers":{"' + $Name + '":{"type":"http","url":"' + $url + '","headers":{"Authorization":"Bearer ' + $Token + '"}}}}'
    $file = Join-Path $dir 'mcping.mcp.json'
    [IO.File]::WriteAllText($file, $json, (New-Object Text.ASCIIEncoding))
    $settings = Join-Path $env:USERPROFILE '.claude.json'
    try {
        Add-ClaudeUserServer $settings $Name $url $Token
        Write-Host ''
        Write-Host 'SUCCESS: this computer is set up for MCPing.' -ForegroundColor Green
        Write-Host ''
        Write-Host 'What is left: quit the Claude app completely (including its icon near the clock) and open it'
        Write-Host 'again. Then ask Claude to take a screenshot with the mcping tools.'
        Write-Host ''
        Write-Host "(Details: the mcping server was added to $settings; a backup of the old file is next to it.)"
        Show-DesktopNote
    } catch {
        Write-Host ''
        Write-Host "Could not update $settings ($($_.Exception.Message))."
        Write-Host 'The device certificate is saved and trusted. To finish, copy this file to the folder you open'
        Write-Host "in Claude as '.mcp.json' (it contains your access token): $file"
    }
    return
}
$cmdline = '"' + $claude + '"'
cmd /c "$cmdline mcp remove --scope user $Name >nul 2>&1"
cmd /c "$cmdline mcp add --transport http --scope user $Name $url --header `"Authorization: Bearer $Token`""
if ($LASTEXITCODE -ne 0) { throw 'claude mcp add failed.' }
Write-Host ''
cmd /c "$cmdline mcp list"
Write-Host ''
Write-Host 'Done. Restart Claude Code if it was open, then ask it to take a screenshot with the mcping tools.'
Show-DesktopNote
