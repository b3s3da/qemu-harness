<#
 qh installer / bootstrapper (Windows, PowerShell 5.1+).  GPL-3.0-or-later.

 One-liner (installs into ~\tools\qemu-harness, asks before installing anything):
     irm https://raw.githubusercontent.com/b3s3da/qemu-harness/main/install.ps1 | iex
 With options:
     & ([scriptblock]::Create((irm https://raw.githubusercontent.com/b3s3da/qemu-harness/main/install.ps1))) -Yes -Arch all
 Prefer to read it first? Save the file, read it, then run:  .\install.ps1

 What it does: (1) gets the harness (zip download, no git needed; skipped when run from a checkout)
               (2) offers to install Python / QEMU / Go through winget when missing
               (3) builds the guest agent + fetches guest userland   (4) adds qh to your user PATH
               (5) installs the agent skill for Claude Code and Grok   (6) optional smoke test

 Options: -Yes (no prompts)  -Arch aarch64|arm|riscv64|x86_64|all  -Dir PATH  -Repo owner/name  -Ref branch-or-tag  -BaseUrl mirror
          -NoDeps (never call winget)  -NoPath  -NoSkills  -Smoke
#>
param(
  [string]$Repo = "b3s3da/qemu-harness", [string]$Ref = "main", [string]$Dir = "$HOME\tools\qemu-harness",
  [string]$Arch = "aarch64", [string]$BaseUrl = "https://github.com", [switch]$Yes, [switch]$NoDeps, [switch]$NoPath, [switch]$NoSkills, [switch]$Smoke
)
$ErrorActionPreference = "Continue"   # native tools print progress on stderr; exit codes are checked explicitly
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
function Ok($m)   { Write-Host "[ok]   $m" -ForegroundColor Green }
function Info($m) { Write-Host "[..]   $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }
function Ask($q) {
  if ($Yes) { return $true }
  if (-not [Environment]::UserInteractive) { return $false }
  try { $a = Read-Host "$q [Y/n]" } catch { return $false }
  return ($a -eq "" -or $a -match "^(y|yes|д|да)$")
}

# ---------------------------------------------------------------- 1. get the harness
$Root = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "qh.py"))) { $Root = $PSScriptRoot; Ok "using checkout at $Root" }
else {
  Info "downloading $Repo@$Ref"
  $tmp = Join-Path $env:TEMP ("qh-dl-" + [guid]::NewGuid().ToString("N").Substring(0,8)); New-Item -ItemType Directory $tmp | Out-Null
  try {
    $zip = Join-Path $tmp "src.zip"
    Invoke-WebRequest -UseBasicParsing "$BaseUrl/$Repo/archive/$Ref.zip" -OutFile $zip
    Expand-Archive $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $src -or -not (Test-Path (Join-Path $src.FullName "qh.py"))) { Fail "downloaded archive does not look like qh (no qh.py)" }
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Copy-Item (Join-Path $src.FullName "*") $Dir -Recurse -Force      # keeps an existing cache\ untouched
    $Root = $Dir; Ok "harness installed to $Dir"
  } catch { Fail "download failed: $($_.Exception.Message)  (check -Repo/-Ref, or clone the repo and run .\install.ps1)" }
  finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- 2. dependencies
$archs = @(if ($Arch -eq "all") { "aarch64","arm","riscv64","x86_64" } else { $Arch })
$qnames = @{ aarch64="qemu-system-aarch64"; arm="qemu-system-arm"; riscv64="qemu-system-riscv64"; x86_64="qemu-system-x86_64" }
foreach ($a in $archs) { if (-not $qnames.ContainsKey($a)) { Fail "unknown arch '$a'" } }
$winget = Get-Command winget -ErrorAction SilentlyContinue

function Install-Dep($label, $wingetId, $why) {
  if ($NoDeps -or -not $winget) { return $false }
  if (-not (Ask "$label is missing ($why). Install it now with winget ($wingetId)?")) { return $false }
  Info "winget install $wingetId (a UAC prompt may appear)"
  & winget install --id $wingetId --exact --silent --accept-package-agreements --accept-source-agreements
  Refresh-Path
  return $true
}
function Find-Python {
  foreach ($n in "python","py") {
    $c = Get-Command $n -ErrorAction SilentlyContinue
    if ($c) { $v = & $c.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null; if ($LASTEXITCODE -eq 0 -and $v -and [version]$v -ge [version]"3.8") { return @{ exe=$c.Source; ver=$v } } }
  }
  return $null
}
function Have-Qemu($a) { return [bool](Get-Command $qnames[$a] -ErrorAction SilentlyContinue) -or (Test-Path "C:\Program Files\qemu\$($qnames[$a]).exe") }

$py = Find-Python
if (-not $py) { if (Install-Dep "Python 3.8+" "Python.Python.3.12" "runs the harness") { $py = Find-Python } }
if (-not $py) { Fail "Python 3.8+ not found. Install it (winget install Python.Python.3.12), open a new terminal and re-run." }
Ok "python $($py.ver) ($($py.exe))"

$missing = @($archs | Where-Object { -not (Have-Qemu $_) })
if ($missing.Count) { [void](Install-Dep "QEMU" "SoftwareFreedomConservancy.QEMU" "emulator for: $($missing -join ', ')") }
$missing = @($archs | Where-Object { -not (Have-Qemu $_) })
if ($missing.Count) { Fail "QEMU missing for: $($missing -join ', '). Install: winget install SoftwareFreedomConservancy.QEMU  (or https://www.qemu.org/download/#windows)" }
Ok "qemu present for: $($archs -join ', ')"

if (-not (Get-Command go -ErrorAction SilentlyContinue)) { [void](Install-Dep "Go" "GoLang.Go" "cross-builds the small guest agent") }
if (-not (Get-Command go -ErrorAction SilentlyContinue)) { Fail "Go not found. Install: winget install GoLang.Go, open a new terminal and re-run." }
Ok (& go version)

# ---------------------------------------------------------------- 3. userland + agent
foreach ($a in $archs) { & $py.exe "$Root\qh.py" setup --arch $a; if ($LASTEXITCODE) { Fail "qh setup --arch $a failed" } }
Ok "guest userland + agent ready"

# ---------------------------------------------------------------- 4. PATH
if (-not $NoPath) {
  $up = [Environment]::GetEnvironmentVariable("Path","User")
  if (($up -split ";") -notcontains $Root) {
    [Environment]::SetEnvironmentVariable("Path", ($up.TrimEnd(";") + ";" + $Root), "User")
    Ok "added $Root to your user PATH (open a NEW terminal to use 'qh')"
  } else { Ok "already on PATH" }
  if (($env:Path -split ";") -notcontains $Root) { $env:Path += ";$Root" }
}

# ---------------------------------------------------------------- 5. agent skill (Claude Code / Grok read SKILL.md from these folders)
if (-not $NoSkills) {
  foreach ($d in @("$HOME\.claude\skills", "$HOME\.grok\skills")) {
    $t = Join-Path $d "qemu-harness"
    New-Item -ItemType Directory -Force $t | Out-Null
    Copy-Item "$Root\skill\SKILL.md" "$t\SKILL.md" -Force
    Ok "skill -> $t"
  }
}

# ---------------------------------------------------------------- 6. smoke test
if ($Smoke) {
  $tmp = Join-Path $env:TEMP ("qh-smoke-" + [guid]::NewGuid().ToString("N").Substring(0,6))
  New-Item -ItemType Directory $tmp | Out-Null; Push-Location $tmp
  try {
    ('{"kernel":"alpine:' + $archs[0] + '","fwd":[]}') | Set-Content qh.json -Encoding ascii
    & $py.exe "$Root\qh.py" up --timeout 120; if ($LASTEXITCODE) { Fail "smoke: up failed" }
    & $py.exe "$Root\qh.py" exec "uname -a"
    & $py.exe "$Root\qh.py" down | Out-Null
    Ok "smoke test passed"
  } finally { Pop-Location; Start-Sleep 1; Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Host "`nDone. In a new terminal:  mkdir lab; cd lab; qh up --kernel alpine:$($archs[0]); qh exec 'uname -a'; qh down" -ForegroundColor Cyan
