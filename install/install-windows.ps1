<#
.SYNOPSIS
  MCPing installer for a Windows PC: firmware tools and client setup.

.DESCRIPTION
  NON-PRODUCTION. FOR TESTING ONLY. NO WARRANTY. AI-generated; not yet verified on a clean machine.
  By running this script you accept the terms in DISCLAIMER.md.

  The MCPing device itself runs on Linux only (see install-device.sh). This script prepares a Windows PC to:
    1. install the tools that build and flash the ESP32-S3 firmware (Python, esptool, arduino-cli, ESP32 core)
    2. build the firmware, and optionally flash it to a board plugged into this PC
    3. connect this PC to an MCPing device: download and verify its certificate authority, trust it, and
       register the device as an MCP server in Claude Code's user settings

  It checks that every tool is present BEFORE it builds, flashes or connects anything, asks before each
  group of changes, shows every default, and explains every error. Nothing needs administrator rights.
  If your organisation blocks scripts, do NOT work around it: ask an administrator, or use the manual steps
  in docs/06-connect.md.

.PARAMETER CheckOnly         Only check this PC; change nothing.
.PARAMETER DryRun            Show what would be done; change nothing.
.PARAMETER Yes               Accept every default without asking (still needs -AcceptDisclaimer).
.PARAMETER AcceptDisclaimer  State that you have read and accept DISCLAIMER.md.
.PARAMETER Address           Device name or IP address (default asked; mcping.local if the network supports it).
.PARAMETER Port              Device MCP port (default 8765).
.PARAMETER CaFingerprint     SHA-256 fingerprint of the device certificate authority, as shown on the device.
                             When given, it is checked automatically; otherwise you are asked to type it.
.PARAMETER ComPort           Serial port of the ESP32 board's UART port, for example COM5, to flash it.
.EXAMPLE
  powershell -File .\install\install-windows.ps1 -CheckOnly -AcceptDisclaimer
#>
[CmdletBinding()]
param(
    [switch]$CheckOnly,
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$AcceptDisclaimer,
    [string]$Address,
    [int]$Port = 8765,
    [string]$CaFingerprint,
    [string]$ComPort
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:Fails = 0
$script:Warns = 0
$RootDir = Split-Path -Parent $PSScriptRoot
$McpingDir = Join-Path $env:USERPROFILE '.mcping'
$LogPath = Join-Path $env:TEMP ("mcping-install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType File -Force -Path $LogPath | Out-Null

# ---------------------------------------------------------------------------------------------- output
function Write-LogLine([string]$Text) { Add-Content -Path $LogPath -Value $Text -Encoding UTF8 }
function Say([string]$Text)  { Write-Host $Text; Write-LogLine $Text }
function Step([string]$Text) { Write-Host ''; Write-Host "== $Text ==" -ForegroundColor Cyan; Write-LogLine "== $Text ==" }
function Ok([string]$Text)   { Write-Host '  [ OK ] ' -ForegroundColor Green -NoNewline; Write-Host $Text; Write-LogLine "  [ OK ] $Text" }
function Warn([string]$Text) { $script:Warns++; Write-Host '  [WARN] ' -ForegroundColor Yellow -NoNewline; Write-Host $Text; Write-LogLine "  [WARN] $Text" }
function Bad([string]$Text)  { $script:Fails++; Write-Host '  [FAIL] ' -ForegroundColor Red -NoNewline; Write-Host $Text; Write-LogLine "  [FAIL] $Text" }

# An error that stops the script, with what to do about it.
class McpingError : System.Exception {
    [string]$Hint
    McpingError([string]$Message, [string]$Hint) : base($Message) { $this.Hint = $Hint }
}
function Stop-Install([string]$Message, [string]$Hint = '') { throw [McpingError]::new($Message, $Hint) }

# ------------------------------------------------------------------------------------------- prompting
function Test-Interactive { return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected }

function Ask([string]$Question, [string]$Default = '') {
    if ($Yes) { Say "  $Question [$Default] (default used)"; return $Default }
    if (-not (Test-Interactive)) { Stop-Install "A question needs an answer but there is no console: $Question" 'Run the script in a PowerShell window, or pass -Yes.' }
    $a = Read-Host "  $Question [$Default]"
    if ([string]::IsNullOrWhiteSpace($a)) { $a = $Default }
    Write-LogLine "  $Question -> $a"
    return $a
}
function AskYN([string]$Question, [bool]$Default = $false) {
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    if ($Yes) { Say "  $Question [$hint] (default used: $(if ($Default) {'yes'} else {'no'}))"; return $Default }
    if (-not (Test-Interactive)) { Stop-Install "A question needs an answer but there is no console: $Question" 'Run the script in a PowerShell window, or pass -Yes.' }
    $a = Read-Host "  $Question [$hint]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $Default }
    Write-LogLine "  $Question -> $a"
    return ($a -match '^[Yy]')
}
function AskText([string]$Question) {
    if ($Yes -or -not (Test-Interactive)) { Stop-Install "This needs your own answer and cannot use a default: $Question" 'Run the script in a PowerShell window without -Yes, or pass the value as a parameter (for example -CaFingerprint).' }
    $a = Read-Host "  $Question"
    Write-LogLine "  $Question -> $a"
    return $a
}
function AskSecret([string]$Question) {
    if (-not (Test-Interactive)) { Stop-Install "A secret is needed but there is no console: $Question" 'Run the script in a PowerShell window, or set the MCPING_TOKEN environment variable first.' }
    $sec = Read-Host "  $Question" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# ---------------------------------------------------------------------------------------- run programs
# Runs a program, appends its output to the log, returns the exit code. Never shows secrets (none are passed).
function Invoke-Native([string]$What, [string]$Exe, [string[]]$Arguments, [switch]$AllowFail) {
    if ($DryRun) { Say "  [dry-run] $What : $Exe $($Arguments -join ' ')"; return 0 }
    Say "  ... $What"
    $out = [IO.Path]::GetTempFileName(); $err = [IO.Path]::GetTempFileName()
    try {
        $argLine = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
        $p = Start-Process -FilePath $Exe -ArgumentList $argLine -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
        $code = $p.ExitCode
        Write-LogLine "  > $Exe $argLine  (exit $code)"
        Get-Content $out, $err -ErrorAction SilentlyContinue | ForEach-Object { Write-LogLine "    $_" }
        if ($code -ne 0 -and -not $AllowFail) {
            Write-Host "  The last lines of its output:" -ForegroundColor Red
            Get-Content $out, $err -Tail 15 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
            Stop-Install "$What failed (exit code $code)." "The command was: $Exe $argLine`n  Full log: $LogPath"
        }
        return $code
    } finally { Remove-Item $out, $err -ErrorAction SilentlyContinue }
}

function Get-NativeOutput([string]$Exe, [string[]]$Arguments) {
    $ErrorActionPreference = 'Continue'      # stderr text from a program must not become an exception
    try { return (& $Exe @Arguments 2>&1 | Out-String).Trim() } catch { return '' }
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

# ------------------------------------------------------------------------------------- finding tools
function Find-Python {
    $candidates = @(
        [pscustomobject]@{ Name = 'py';      Prefix = @('-3') },
        [pscustomobject]@{ Name = 'python';  Prefix = @() },
        [pscustomobject]@{ Name = 'python3'; Prefix = @() }
    )
    foreach ($cand in $candidates) {
        $cmd = Get-Command $cand.Name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        if ($cmd.Source -like '*\WindowsApps\*' -and $cand.Name -ne 'py') {
            # the Microsoft Store stub opens the Store instead of running Python
            $v = Get-NativeOutput $cmd.Source @('--version'); if ($v -notmatch '^Python 3') { continue }
        }
        # no quote characters in the code: Windows PowerShell 5.1 strips them from native-program arguments
        $v = Get-NativeOutput $cmd.Source (@($cand.Prefix) + @('-c', 'import sys; print(*sys.version_info[:3])'))
        if ($v -match '^(\d+) (\d+) (\d+)$' -and ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10))) {
            return [pscustomobject]@{ Exe = $cmd.Source; Prefix = @($cand.Prefix); Version = ($v -replace ' ', '.') }
        }
    }
    return $null
}
function Find-ArduinoCli {
    $c = Get-Command arduino-cli -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Microsoft\WinGet\Links\arduino-cli.exe", "$env:ProgramFiles\Arduino CLI\arduino-cli.exe", "$env:USERPROFILE\tools\arduino-cli\arduino-cli.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

# ---------------------------------------------------------------------------------------- C# helpers
# Used only for (1) downloading the device's public certificate authority before it can be trusted, and
# (2) calling the device while trusting ONLY that authority (the whole chain must end in it).
$csharp = @'
using System;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
using System.Text;

public static class McpingNet
{
    static McpingNet() { ServicePointManager.SecurityProtocol = (SecurityProtocolType)3072; }   // TLS 1.2

    // Fetch a PUBLIC file without validating the server certificate (used once, for the CA file itself).
    public static byte[] DownloadUnverified(string url, int timeoutMs)
    {
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.Timeout = timeoutMs;
        req.ServerCertificateValidationCallback = delegate (object s, X509Certificate c, X509Chain ch, SslPolicyErrors e) { return true; };
        using (WebResponse resp = req.GetResponse())
        using (Stream st = resp.GetResponseStream())
        using (MemoryStream ms = new MemoryStream())
        {
            st.CopyTo(ms);
            return ms.ToArray();
        }
    }

    // Call the device, accepting only a certificate chain that ends in the given CA.
    public static int Request(string method, string url, string token, string body, byte[] caDer, int timeoutMs)
    {
        X509Certificate2 ca = new X509Certificate2(caDer);
        HttpWebRequest req = (HttpWebRequest)WebRequest.Create(url);
        req.Method = method;
        req.Timeout = timeoutMs;
        if (token != null && token.Length > 0) req.Headers["Authorization"] = "Bearer " + token;
        req.ServerCertificateValidationCallback = delegate (object s, X509Certificate c, X509Chain ch, SslPolicyErrors e)
        {
            if ((e & SslPolicyErrors.RemoteCertificateNameMismatch) != 0) return false;
            X509Chain chain = new X509Chain();
            chain.ChainPolicy.RevocationMode = X509RevocationMode.NoCheck;
            chain.ChainPolicy.VerificationFlags = X509VerificationFlags.AllowUnknownCertificateAuthority;
            chain.ChainPolicy.ExtraStore.Add(ca);
            chain.Build(new X509Certificate2(c));
            if (chain.ChainElements.Count == 0) return false;
            X509Certificate2 root = chain.ChainElements[chain.ChainElements.Count - 1].Certificate;
            return string.Equals(root.Thumbprint, ca.Thumbprint, StringComparison.OrdinalIgnoreCase);
        };
        if (body != null)
        {
            byte[] data = Encoding.UTF8.GetBytes(body);
            req.ContentType = "application/json";
            req.Accept = "application/json, text/event-stream";
            req.ContentLength = data.Length;
            using (Stream rs = req.GetRequestStream()) { rs.Write(data, 0, data.Length); }
        }
        try
        {
            using (HttpWebResponse resp = (HttpWebResponse)req.GetResponse()) { return (int)resp.StatusCode; }
        }
        catch (WebException ex)
        {
            HttpWebResponse r = ex.Response as HttpWebResponse;
            if (r != null) { int code = (int)r.StatusCode; r.Close(); return code; }
            throw;
        }
    }
}
'@

# ================================================================================================ start
Say "MCPing installer for Windows  (log: $LogPath)"
Say "Project folder: $RootDir"
if ($DryRun) { Say 'DRY RUN: nothing will be changed.' }
if ($CheckOnly) { Say 'CHECK ONLY: nothing will be changed.' }

try {
    Step 'NON-PRODUCTION, TESTING ONLY'
    Say '  This software is AI-generated, not independently reviewed and offered with NO WARRANTY.'
    Say '  It lets an AI or a person see and operate another computer''s keyboard and mouse.'
    Say '  Use it only on equipment you own or are explicitly allowed to control, entirely at your own risk.'
    Say '  Full terms: DISCLAIMER.md in this project.'
    if (-not $AcceptDisclaimer) {
        if (-not (Test-Interactive)) { Stop-Install 'You must accept DISCLAIMER.md.' 'Read it, then run again with -AcceptDisclaimer.' }
        $a = Read-Host '  Type  I AGREE  to accept DISCLAIMER.md and continue'
        if ($a -cne 'I AGREE') { Stop-Install 'The disclaimer was not accepted.' 'Read DISCLAIMER.md. Nothing was changed.' }
    }
    Ok 'Disclaimer accepted'

    # ------------------------------------------------------------------------------------------ checks
    Step '1. Checking this PC'
    if ($PSVersionTable.PSVersion.Major -lt 5) { Bad "PowerShell $($PSVersionTable.PSVersion) is too old; Windows PowerShell 5.1 or newer is needed." } else { Ok "PowerShell $($PSVersionTable.PSVersion)" }
    if ($env:OS -ne 'Windows_NT') { Bad 'This script is for Windows. On Linux use install-device.sh.' } else { Ok "Windows: $([Environment]::OSVersion.VersionString)" }
    if (-not [Environment]::Is64BitOperatingSystem) { Bad 'A 64-bit Windows is required.' } else { Ok '64-bit Windows' }
    if ($ExecutionContext.SessionState.LanguageMode -ne 'FullLanguage') {
        Bad "PowerShell is running in '$($ExecutionContext.SessionState.LanguageMode)' mode (a security policy is restricting it), so this script cannot work here."
        Stop-Install 'A policy on this PC restricts PowerShell. This is deliberate, so the script will not try to get around it.' 'Ask the administrator, or use the manual steps in docs/06-connect.md (certificate download, NODE_EXTRA_CA_CERTS, claude mcp add).'
    } else { Ok 'PowerShell language mode: FullLanguage' }

    $drive = (Get-Item $env:USERPROFILE).PSDrive
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -ge 3) { Ok "Free disk space on $($drive.Name): ${freeGb} GB" } else { Bad "Only ${freeGb} GB free on $($drive.Name): (about 3 GB is needed for Python, the ESP32 toolchain and builds)." }

    $online = $false
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; $r = Invoke-WebRequest -Uri 'https://pypi.org/simple/pip/' -UseBasicParsing -TimeoutSec 15; $online = ($r.StatusCode -eq 200) } catch { Write-LogLine "  network test: $($_.Exception.Message)" }
    if ($online) { Ok 'Network: can reach pypi.org' } else { Bad 'Cannot reach https://pypi.org. Check the internet connection, the company proxy or firewall, or try another network. Tools cannot be downloaded without it.' }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) { Ok 'winget is available (used to install Python and arduino-cli)' } else { Warn 'winget was not found. Missing tools cannot be installed automatically: install "App Installer" from the Microsoft Store, or install Python 3.10+ and arduino-cli by hand.' }

    $firmwareDir = Join-Path $RootDir 'firmware\mcping_hid'
    if (Test-Path (Join-Path $firmwareDir 'mcping_hid.ino')) { Ok 'Found firmware\mcping_hid\mcping_hid.ino' } else { Warn "Firmware sketch not found at $firmwareDir. Building and flashing will be skipped. Run the script from a complete copy of the project (install\install-windows.ps1)." }

    # ----------------------------------------------------------------------------------------- questions
    Step '2. Your choices (press Enter to accept a default)'
    $doBuild = $false; $doFlash = $false
    if ($CheckOnly) {
        $doTools = $true; $doConnect = $false
        Say '  (check-only: no questions asked; the firmware tools are checked)'
    } else {
        $doTools = AskYN 'Install/check the firmware tools (Python, esptool, arduino-cli, ESP32 core)?' $true
        if ($doTools -and (Test-Path $firmwareDir)) {
            $doBuild = AskYN 'Build the ESP32 firmware?' $true
            if ($doBuild) { $doFlash = AskYN 'Flash the firmware to a board plugged into this PC?' ([bool]$ComPort) }
            elseif ($ComPort) { Warn 'A -ComPort was given but building is switched off, so nothing will be flashed.' }
        }
        $doConnect = AskYN 'Connect this PC to an MCPing device (trust its certificate, register it as an MCP server)?' $true
    }

    # ------------------------------------------------------------------------------ dependency check
    Step '3. Checking the tools'
    $py = $null; $arduino = $null
    function Get-Status {
        $s = [ordered]@{}
        $s.python = Find-Python
        $s.arduino = Find-ArduinoCli
        $s.pip = $false; $s.esptool = $false; $s.core = $false
        if ($s.python) {
            $pipOut = Get-NativeOutput $s.python.Exe ($s.python.Prefix + @('-m', 'pip', '--version'))
            $s.pip = [bool]($pipOut -match '^pip ')
            $esp = Get-NativeOutput $s.python.Exe ($s.python.Prefix + @('-m', 'esptool', 'version'))
            $s.esptool = [bool]($esp -match 'esptool')
        }
        if ($s.arduino) {
            $core = Get-NativeOutput $s.arduino @('core', 'list')
            $s.core = [bool]($core -match 'esp32:esp32')
        }
        $s.claude = Get-Command claude -ErrorAction SilentlyContinue
        $s.curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        return $s
    }
    function Show-Status($s) {
        if ($doTools) {
            if ($s.python) { Ok "Python $($s.python.Version) ($($s.python.Exe))" } else { Bad 'Python 3.10 or newer is not installed.' }
            if ($s.pip)    { Ok 'pip' } else { Bad 'pip is not available.' }
            if ($s.esptool) { Ok 'esptool' } else { Bad 'esptool is not installed (needed to flash the ESP32).' }
            if ($s.arduino) { Ok "arduino-cli ($($s.arduino))" } else { Bad 'arduino-cli is not installed.' }
            if ($s.core)   { Ok 'Arduino ESP32 core (esp32:esp32)' } else { Bad 'The Arduino ESP32 core is not installed.' }
        }
        if ($s.claude) { Ok "Claude Code command found ($($s.claude.Source))" } else { Warn 'The "claude" command was not found. Registration will edit the settings file directly, which also works for the Claude desktop app''s Code tab.' }
    }
    $before = $script:Fails
    $st = Get-Status
    Show-Status $st
    $missing = $script:Fails - $before

    if ($CheckOnly) {
        Step 'Result'
        Say "  $($script:Fails) problem(s), $($script:Warns) warning(s)."
        if ($script:Fails -eq 0) { Say '  This PC looks ready. Run the script again without -CheckOnly to continue.' } else { Say '  Fix the FAIL lines above, or run the installer without -CheckOnly: it installs the missing tools.' }
        if ($script:Fails -eq 0) { exit 0 } else { exit 2 }
    }

    if (-not $online -and $missing -gt 0) { Stop-Install 'Tools are missing and the network is not reachable, so they cannot be installed.' 'Connect to the internet (or fix the proxy), or install the missing tools by hand, then run the script again.' }

    # ------------------------------------------------------------------------------------ install tools
    if ($doTools -and $missing -gt 0) {
        Step '4. Installing missing tools'
        if (-not $st.python) {
            if (-not $winget) { Stop-Install 'Python 3.10+ is missing and winget is not available.' 'Install Python 3.12 from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then run this script again.' }
            if (AskYN 'Install Python 3.12 with winget?' $true) {
                [void](Invoke-Native 'install Python 3.12' $winget.Source @('install', '--id', 'Python.Python.3.12', '-e', '--accept-source-agreements', '--accept-package-agreements'))
            } else { Stop-Install 'Python is required for the firmware tools.' 'Install Python 3.10 or newer yourself, or run again and choose not to install the firmware tools.' }
        }
        if (-not $st.arduino) {
            if (-not $winget) { Stop-Install 'arduino-cli is missing and winget is not available.' 'Download arduino-cli from https://github.com/arduino/arduino-cli/releases, unpack it, add its folder to PATH, then run this script again.' }
            if (AskYN 'Install arduino-cli with winget (package ArduinoSA.CLI)?' $true) {
                [void](Invoke-Native 'install arduino-cli' $winget.Source @('install', '--id', 'ArduinoSA.CLI', '-e', '--accept-source-agreements', '--accept-package-agreements'))
            } else { Stop-Install 'arduino-cli is required to build the firmware.' 'Install it yourself or choose not to build.' }
        }
        if (-not $DryRun) {
            Update-SessionPath
            $py = Find-Python
            if (-not $py) { Stop-Install 'Python was installed but cannot be found in this window yet.' 'Close this window, open a new PowerShell and run the script again so the new PATH is used.' }
            if (-not $st.pip) { [void](Invoke-Native 'enable pip' $py.Exe ($py.Prefix + @('-m', 'ensurepip', '--upgrade'))) }
            if (-not $st.esptool) { [void](Invoke-Native 'install esptool 5 or newer' $py.Exe ($py.Prefix + @('-m', 'pip', 'install', '--user', '--upgrade', 'esptool>=5'))) }
            $arduino = Find-ArduinoCli
            if (-not $arduino) { Stop-Install 'arduino-cli was installed but cannot be found in this window yet.' 'Close this window, open a new PowerShell and run the script again.' }
            if (-not $st.core) {
                Say '  The ESP32 core is a large download (several hundred MB).'
                if (AskYN 'Download and install the Arduino ESP32 core 3.3.12?' $true) {
                    [void](Invoke-Native 'initialise arduino-cli' $arduino @('config', 'init') -AllowFail)
                    [void](Invoke-Native 'add the ESP32 package index' $arduino @('config', 'add', 'board_manager.additional_urls', 'https://espressif.github.io/arduino-esp32/package_esp32_index.json') -AllowFail)
                    [void](Invoke-Native 'update the board index' $arduino @('core', 'update-index'))
                    [void](Invoke-Native 'install the ESP32 core' $arduino @('core', 'install', 'esp32:esp32@3.3.12'))
                } else { Stop-Install 'The ESP32 core is required to build the firmware.' 'Run again and agree, or choose not to build.' }
            }
        }
        Step '4b. Checking every tool again before continuing'
        if ($DryRun) { Say '  [dry-run] would re-check all tools here' } else {
            $before = $script:Fails
            $st = Get-Status
            Show-Status $st
            if ($script:Fails -gt $before) { Stop-Install 'A tool is still missing after installation (see the FAIL lines above).' 'Close this window and open a new PowerShell (so the new PATH applies), then run the script again. Nothing has been built, flashed or connected.' }
        }
    } else { Ok 'All requested tools are present' }

    if ($st.python) { $py = $st.python }
    if ($st.arduino) { $arduino = $st.arduino }

    # ---------------------------------------------------------------------------------- build firmware
    $outDir = Join-Path $McpingDir 'firmware-build'
    $mergedBin = Join-Path $outDir 'mcping_hid.ino.merged.bin'
    if ($doBuild) {
        Step '5. Building the firmware'
        $flashSize = Ask 'Flash size of your ESP32 board (16M for an N16R8 board; check your board''s name)' '16M'
        if ($flashSize -notmatch '^(4|8|16|32)M$') { Stop-Install "Flash size '$flashSize' is not one of 4M, 8M, 16M, 32M." 'Use the size printed on your board (N16R8 means 16 MB flash, 8 MB RAM).' }
        $ledPin = Ask 'GPIO pin of the board''s RGB LED (48 for most boards, 38 for v1.1 boards)' '48'
        if ($ledPin -notmatch '^\d{1,2}$') { Stop-Install "LED pin '$ledPin' is not a number." 'Use 48 or 38 unless your board''s documentation says otherwise.' }
        if (-not $DryRun) { New-Item -ItemType Directory -Force -Path $outDir | Out-Null }
        $fqbn = "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,FlashSize=$flashSize"
        $compileArgs = @('compile', '--fqbn', $fqbn, '--output-dir', $outDir)
        if ($ledPin -ne '48') { $compileArgs += @('--build-property', "compiler.cpp.extra_flags=-DLED_PIN=$ledPin") }
        $compileArgs += $firmwareDir
        [void](Invoke-Native 'compile the firmware' $arduino $compileArgs)
        if (-not $DryRun) {
            if (Test-Path $mergedBin) { Ok "Built: $mergedBin" } else { Stop-Install 'The compile finished but the output file was not found.' "Look in $outDir and in the log for the real file name." }
        }
    }

    # ----------------------------------------------------------------------------------- flash firmware
    if ($doFlash) {
        Step '6. Flashing the board'
        Say '  Plug the board''s UART port (the one next to the USB-serial chip) into this PC.'
        $ports = @()
        try { $ports = @(Get-CimInstance Win32_PnPEntity -ErrorAction Stop | Where-Object { $_.Name -match '\(COM\d+\)' } | ForEach-Object { $_.Name }) } catch { Write-LogLine "  port list: $($_.Exception.Message)" }
        if ($ports.Count -eq 0) { Stop-Install 'No serial (COM) ports were found.' 'Plug in the ESP32 board using its UART USB-C port with a DATA cable. If Windows shows an unknown device, install the driver for the board''s USB-serial chip (CH343/CH340/CP210x).' }
        Say '  Serial ports found:'; $ports | ForEach-Object { Say "    $_" }
        $port = $ComPort
        if (-not $port) { $port = Ask 'Which COM port is the board''s UART port (for example COM5)?' '' }
        if ($port -notmatch '^COM\d+$') { Stop-Install "'$port' is not a COM port name." 'Type it like COM5, as shown in the list above.' }
        if (-not ($ports -match "\($port\)")) { Stop-Install "$port is not in the list of ports found." 'Check the port number in the list above and that the board is still connected.' }
        $confirmed = if ($Yes -and $ComPort) { $true } else { AskYN "Write the firmware to the board on $port now? This replaces whatever is on that board." $false }
        if (-not $confirmed) { Say '  Flashing skipped.' }
        else {
            [void](Invoke-Native 'write the firmware' $py.Exe ($py.Prefix + @('-m', 'esptool', '--chip', 'esp32s3', '-p', $port, '-b', '460800', '--after', 'hard-reset', 'write-flash', '0x0', $mergedBin)))
            Ok 'Firmware written. Unplug and replug the board, then check the LED (docs/03-build.md). If flashing cannot connect: hold BOOT, tap RST, release BOOT, and run again.'
        }
    }

    # ----------------------------------------------------------------------------------- connect client
    if ($doConnect) {
        Step '7. Connecting this PC to an MCPing device'
        Say '  You need: the device''s address, its access token, and the certificate fingerprint shown on the device'
        Say '  (MCPing Certificate app, or: mcping-cert status).'
        if (-not $Address) { $Address = Ask 'Device name or IP address (use the IP address if mcping.local does not work on your network)' 'mcping.local' }
        if ($Address -notmatch '^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$') { Stop-Install "'$Address' is not a valid name or address." 'Use a host name such as mcping.local or an IP address such as 192.0.2.10 (no https://, no slashes).' }
        if ($Port -lt 1 -or $Port -gt 65535) { Stop-Install "Port $Port is not valid." 'The default is 8765.' }

        try { $ips = [Net.Dns]::GetHostAddresses($Address); Ok "Name resolves: $Address -> $(($ips | ForEach-Object { $_.IPAddressToString }) -join ', ')" }
        catch { Stop-Install "The name '$Address' does not resolve to an address." "Many networks and managed PCs do not support .local names. Use the device's IP address instead (see docs/troubleshooting.md, known issue 1). Detail: $($_.Exception.Message)" }

        foreach ($pt in @(443, $Port)) {
            $tcp = New-Object Net.Sockets.TcpClient
            try {
                $iar = $tcp.BeginConnect($Address, $pt, $null, $null)
                if ($iar.AsyncWaitHandle.WaitOne(5000) -and $tcp.Connected) { Ok "Port $pt on $Address is reachable" }
                else { Stop-Install "Port $pt on $Address did not answer within 5 seconds." "Check that the device is on and on this network, that the services are running (systemctl status mcping mcping-portal), and that no firewall blocks port $pt." }
            } finally { $tcp.Close() }
        }

        if (-not ('McpingNet' -as [type])) { Add-Type -TypeDefinition $csharp -Language CSharp }
        Say '  Downloading the device''s certificate authority (a public file). It is NOT trusted until you confirm its fingerprint.'
        try { $caBytes = [McpingNet]::DownloadUnverified("https://${Address}/downloads/mcping-ca.crt", 15000) }
        catch { Stop-Install "Could not download the certificate authority from https://${Address}/downloads/mcping-ca.crt." "Is the setup portal running on port 443 of the device? Detail: $($_.Exception.Message)" }
        $pem = [Text.Encoding]::ASCII.GetString($caBytes)
        if ($pem -notmatch '-----BEGIN CERTIFICATE-----') { Stop-Install 'The downloaded file is not a certificate.' 'Something other than the MCPing portal answered on port 443. Check the address.' }
        $b64 = (($pem -split "`r?`n") | Where-Object { $_ -and $_ -notmatch '^-----' }) -join ''
        try { $der = [Convert]::FromBase64String($b64) } catch { Stop-Install 'The downloaded certificate is damaged.' 'Try again; if it repeats, export the CA on the device (MCPing Certificate app) and copy it here by hand.' }
        $fp = ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($der)) -replace '-', '').ToLower()
        Say "  Certificate authority fingerprint (SHA-256):"
        Say "    $fp"
        $expected = $CaFingerprint
        if (-not $expected) {
            Say '  Compare it with the fingerprint shown ON THE DEVICE (not on any web page). Type the first 12 characters shown there.'
            $expected = AskText 'Fingerprint from the device'
        }
        $expected = ($expected -replace '[^0-9a-fA-F]', '').ToLower()
        if ($expected.Length -lt 12) { Stop-Install 'At least 12 hexadecimal characters of the fingerprint are needed to compare.' 'Read them from the MCPing Certificate app on the device, or run mcping-cert status there.' }
        if (-not $fp.StartsWith($expected)) { Stop-Install 'The fingerprint does NOT match the one you gave. The certificate was not trusted and nothing was saved.' 'This can mean the wrong device, a re-issued CA, or someone on the network answering for the device. Check the address and the fingerprint on the device, then try again.' }
        Ok 'Fingerprint matches the one from the device'

        $caPath = Join-Path $McpingDir 'mcping-ca.crt'
        if (-not $DryRun) {
            New-Item -ItemType Directory -Force -Path $McpingDir | Out-Null
            [IO.File]::WriteAllText($caPath, ($pem.Trim() + "`n"), (New-Object Text.ASCIIEncoding))
        }
        Ok "Saved the certificate authority to $caPath"

        $existing = [Environment]::GetEnvironmentVariable('NODE_EXTRA_CA_CERTS', 'User')
        if ($existing -and $existing -ne $caPath) {
            Warn "NODE_EXTRA_CA_CERTS already points to $existing. It can hold only one file; replacing it could break other tools."
            $setEnv = AskYN 'Replace it with the MCPing certificate file?' $false
        } else { $setEnv = AskYN 'Set NODE_EXTRA_CA_CERTS for your user so Claude Code trusts the device?' $true }
        if ($setEnv) {
            if (-not $DryRun) { [Environment]::SetEnvironmentVariable('NODE_EXTRA_CA_CERTS', $caPath, 'User'); $env:NODE_EXTRA_CA_CERTS = $caPath }
            Ok 'NODE_EXTRA_CA_CERTS set (programs started from now on will see it; restart Claude)'
        } else { Say '  Not set. Claude Code will reject the device until NODE_EXTRA_CA_CERTS points to the saved file.' }

        $token = $env:MCPING_TOKEN
        if (-not $token) { $token = AskSecret 'Access token (on the device: sudo cat /etc/mcping/mcping.env, or use an invite)' }
        if (-not $token -or $token.Length -lt 24 -or $token -notmatch '^[A-Za-z0-9._~-]+$') { Stop-Install 'That does not look like a device token (24 or more letters, digits or - _ . ~).' 'Copy the whole token. It is shown once for per-computer tokens; create a new invite on the device if it was lost.' }

        if (-not $DryRun) {
            Say '  Checking the connection, trusting only the device''s authority ...'
            try { $code = [McpingNet]::Request('GET', "https://${Address}:${Port}/health", $null, $null, $der, 15000) }
            catch { Stop-Install "Could not make a trusted connection to https://${Address}:${Port}/health." "The certificate may not cover '$Address' (use the name or IP the device certificate lists: mcping-cert status), or the CA is wrong. Detail: $($_.Exception.Message)" }
            if ($code -ne 200) { Stop-Install "The device answered /health with HTTP $code." 'Check that the MCP service is running on the device.' }
            Ok 'Trusted connection works (/health = 200)'
            $init = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"mcping-installer","version":"1"}}}'
            try { $code = [McpingNet]::Request('POST', "https://${Address}:${Port}/mcp", $token, $init, $der, 20000) }
            catch { Stop-Install 'The MCP endpoint did not answer.' "Detail: $($_.Exception.Message)" }
            if ($code -eq 401) { Stop-Install 'The device refused the token (HTTP 401).' 'Check the token, or whether that computer''s token was revoked. Per-computer tokens are shown once.' }
            if ($code -eq 409) { Stop-Install 'The device is in use by another client (HTTP 409).' 'Only one client can connect at a time. Wait until it disconnects (it frees itself after 5 minutes idle) and try again.' }
            if ($code -ge 400) { Stop-Install "The MCP endpoint answered HTTP $code." 'See journalctl -u mcping on the device.' }
            Ok "MCP endpoint accepted the token (HTTP $code)"
        }

        if (AskYN 'Register the device in Claude Code''s user settings (~\.claude.json)? The token is stored there in plain text.' $true) {
            $settings = Join-Path $env:USERPROFILE '.claude.json'
            $entry = [pscustomobject]@{ type = 'http'; url = "https://${Address}:${Port}/mcp"; headers = [pscustomobject]@{ Authorization = "Bearer $token" } }
            if ($DryRun) { Say "  [dry-run] would add mcpServers.mcping to $settings" } else {
                $cfg = $null
                if (Test-Path $settings) {
                    $backup = "$settings.mcping-backup-$(Get-Date -Format 'yyyyMMddHHmmss')"
                    Copy-Item $settings $backup -Force
                    Say "  Backup of the old settings: $backup"
                    try { $cfg = Get-Content $settings -Raw -Encoding UTF8 | ConvertFrom-Json }
                    catch { Stop-Install "$settings is not valid JSON, so it was left alone." "Fix or move that file, then run again. The backup is $backup." }
                }
                if (-not $cfg) { $cfg = [pscustomobject]@{} }
                if (-not ($cfg.PSObject.Properties.Name -contains 'mcpServers') -or -not $cfg.mcpServers) {
                    $cfg | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{}) -Force
                }
                $cfg.mcpServers | Add-Member -NotePropertyName 'mcping' -NotePropertyValue $entry -Force
                $tmp = "$settings.tmp"
                [IO.File]::WriteAllText($tmp, (ConvertTo-Json -InputObject $cfg -Depth 100), (New-Object Text.UTF8Encoding($false)))
                Move-Item $tmp $settings -Force
                Ok "Registered 'mcping' in $settings"
            }
        } else {
            Say "  Not registered. To do it yourself, add this under 'mcpServers' (replace <token>):"
            Say "    `"mcping`": { `"type`": `"http`", `"url`": `"https://${Address}:${Port}/mcp`", `"headers`": { `"Authorization`": `"Bearer <token>`" } }"
        }
        $token = $null
    }

    # --------------------------------------------------------------------------------------------- done
    Step 'Done'
    Say "  Problems: $($script:Fails)   Warnings: $($script:Warns)"
    Say "  Log: $LogPath (it never contains the token)"
    Say ''
    Say '  Next steps'
    Say '    1. Quit and reopen Claude completely so it sees the new settings and certificate variable.'
    Say '    2. Ask it to take a screenshot with the mcping tools.'
    Say '    3. Read SECURITY.md. Never expose the device to the internet.'
    exit 0
}
catch [McpingError] {
    $e = $_.Exception
    Write-Host ''
    Write-Host "ERROR: $($e.Message)" -ForegroundColor Red
    Write-LogLine "ERROR: $($e.Message)"
    if ($e.Hint) { Write-Host "What to try: $($e.Hint)" -ForegroundColor Yellow; Write-LogLine "What to try: $($e.Hint)" }
    Write-Host "Full log: $LogPath"
    Write-Host 'Nothing is rolled back. Fix the cause and run the script again: it is safe to repeat.'
    exit 1
}
catch {
    Write-Host ''
    Write-Host "UNEXPECTED ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  at $($_.InvocationInfo.ScriptName):$($_.InvocationInfo.ScriptLineNumber)  $($_.InvocationInfo.Line.Trim())"
    Write-LogLine "UNEXPECTED ERROR: $($_.Exception.Message) at line $($_.InvocationInfo.ScriptLineNumber)"
    Write-Host "Full log: $LogPath"
    Write-Host 'If this repeats, keep the log and see docs/troubleshooting.md.'
    exit 1
}
