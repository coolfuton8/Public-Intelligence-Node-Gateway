# MCPing: connect OpenCode on this computer to the device at @@ADDRESS@@ (and optionally Ollama)
#
#   powershell -ExecutionPolicy Bypass -File .\setup-opencode.ps1 [-Model qwen3-mcping] [-NoOllama]
#
# It saves the device certificate authority (CA), trusts it for OpenCode, stores the token in the
# MCPING_TOKEN user environment variable and merges the MCP server (and, unless -NoOllama, an
# Ollama provider) into OpenCode's config. An existing config is backed up first.
param(
    [string]$Token = $env:MCPING_TOKEN,
    [string]$Model = 'qwen3-mcping',
    [string]$OllamaUrl = 'http://localhost:11434/v1',
    [switch]$NoOllama,
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.config\opencode\opencode.json'),
    [string]$Address = '@@ADDRESS@@',
    [switch]$NoEnvVars   # do not touch user environment variables (testing)
)
$ErrorActionPreference = 'Stop'

$url = "https://${Address}:@@PORT@@/mcp"
$caPem = @'
@@CA_PEM@@
'@
$expected = '@@FINGERPRINT@@'

if (-not $Token) { $Token = '@@TOKEN@@' }   # filled in only for a script obtained with a one-time invite
if (-not $Token) {
    $sec = Read-Host 'Device token (on the device: MCPing Connect > Copy token only)' -AsSecureString
    $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
}
if ($Token.Length -lt 24) { throw 'That does not look like a device token (too short).' }

# 1. CA file, checked against the fingerprint this script was generated with
$der = [Convert]::FromBase64String((($caPem -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^-----' }) -join ''))
$got = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($der)) -replace '-', '').ToLower()
if ($got -ne $expected) { throw "Certificate fingerprint mismatch ($got). Download the script again." }
$dir = Join-Path $env:USERPROFILE '.mcping'
New-Item -ItemType Directory -Force $dir | Out-Null
$ca = Join-Path $dir 'mcping-ca.crt'
[IO.File]::WriteAllText($ca, ($caPem.Trim() + "`n"), (New-Object Text.ASCIIEncoding))
Write-Host "Device CA saved to $ca  (SHA-256 $got)"

# 2. environment: CA for Node/Bun based tools, token for the config's {env:MCPING_TOKEN}
if (-not $NoEnvVars) {
    [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $ca, 'User')
    [Environment]::SetEnvironmentVariable('MCPING_TOKEN', $Token, 'User')
    Write-Host 'Set the user environment variables NODE_EXTRA_CA_CERTS and MCPING_TOKEN (new terminals only).'
}

# 3. merge into OpenCode's config
function Set-Prop($o, [string]$n, $v) {
    if ($o.PSObject.Properties[$n]) { $o.$n = $v } else { $o | Add-Member -NotePropertyName $n -NotePropertyValue $v }
}
$cfg = [pscustomobject]@{}
if (Test-Path $ConfigPath) {
    Copy-Item $ConfigPath "$ConfigPath.bak" -Force
    $raw = Get-Content $ConfigPath -Raw
    if ($raw.Trim()) { $cfg = $raw | ConvertFrom-Json }
}
if (-not $cfg.PSObject.Properties['$schema']) { Set-Prop $cfg '$schema' 'https://opencode.ai/config.json' }
if (-not $cfg.PSObject.Properties['mcp']) { Set-Prop $cfg 'mcp' ([pscustomobject]@{}) }
Set-Prop $cfg.mcp 'mcping' ([pscustomobject]@{
    type = 'remote'; url = $url; enabled = $true
    headers = [pscustomobject]@{ Authorization = 'Bearer {env:MCPING_TOKEN}' }
})
if (-not $NoOllama) {
    if (-not $cfg.PSObject.Properties['provider']) { Set-Prop $cfg 'provider' ([pscustomobject]@{}) }
    if (-not $cfg.provider.PSObject.Properties['ollama']) {
        $models = [pscustomobject]@{}
        Set-Prop $models $Model ([pscustomobject]@{ name = $Model })
        Set-Prop $cfg.provider 'ollama' ([pscustomobject]@{
            npm = '@ai-sdk/openai-compatible'; name = 'Ollama (local)'
            options = [pscustomobject]@{ baseURL = $OllamaUrl }
            models = $models
        })
    }
    if (-not $cfg.PSObject.Properties['model']) { Set-Prop $cfg 'model' "ollama/$Model" }
}
New-Item -ItemType Directory -Force (Split-Path $ConfigPath) | Out-Null
[IO.File]::WriteAllText($ConfigPath, ($cfg | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
Write-Host "OpenCode config updated: $ConfigPath"
Write-Host ''
Write-Host 'Next: open a NEW terminal, start Ollama, run: opencode'
