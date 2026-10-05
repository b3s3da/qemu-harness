<#
 qh installer (Windows / PowerShell 5.1+).  GPL-3.0-or-later.
   .\install.ps1                 check deps, build agent, add qh to PATH, install agent skill
   -Arch all|aarch64|arm|riscv64|x86_64   which guest userlands to prepare (default aarch64)
   -NoPath  -NoSkills  -Smoke (boot an Alpine guest and run a command)
#>
param([string]$Arch = "aarch64", [switch]$NoPath, [switch]$NoSkills, [switch]$Smoke)
$ErrorActionPreference = "Continue"  # native tools print progress on stderr; we check exit codes explicitly
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
function Ok($m)   { Write-Host "[ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[fail] $m" -ForegroundColor Red; exit 1 }

# --- Python
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Fail "Python 3.8+ not found. Install it: winget install Python.Python.3.12" }
$ver = & $py.Source -c "import sys;print('%d.%d'%sys.version_info[:2])"
if ([version]$ver -lt [version]"3.8") { Fail "Python $ver is too old (need 3.8+)" }
Ok "python $ver ($($py.Source))"

# --- QEMU
$archs = @(if ($Arch -eq "all") { "aarch64","arm","riscv64","x86_64" } else { $Arch })
$qnames = @{ aarch64="qemu-system-aarch64"; arm="qemu-system-arm"; riscv64="qemu-system-riscv64"; x86_64="qemu-system-x86_64" }
foreach ($a in $archs) {
  $n = $qnames[$a]
  $found = (Get-Command $n -ErrorAction SilentlyContinue)
  if (-not $found -and -not (Test-Path "C:\Program Files\qemu\$n.exe")) { Fail "$n not found. Install QEMU: winget install SoftwareFreedomConservancy.QEMU (or https://www.qemu.org/download/#windows)" }
}
Ok "qemu present for: $($archs -join ', ')"

# --- Go (builds the guest agent)
if (-not (Get-Command go -ErrorAction SilentlyContinue)) { Fail "Go not found (needed to cross-build the guest agent). Install: winget install GoLang.Go" }
Ok ((& go version))

# --- userland + agent
foreach ($a in $archs) { & $py.Source "$Root\qh.py" setup --arch $a; if ($LASTEXITCODE) { Fail "qh setup --arch $a failed" } }
Ok "guest userland + agent ready"

# --- PATH
if (-not $NoPath) {
  $up = [Environment]::GetEnvironmentVariable("Path","User")
  if (($up -split ";") -notcontains $Root) {
    [Environment]::SetEnvironmentVariable("Path", ($up.TrimEnd(";") + ";" + $Root), "User")
    Ok "added $Root to your user PATH (open a NEW terminal to use 'qh')"
  } else { Ok "already on PATH" }
}

# --- agent skill (Claude Code / Grok read SKILL.md from these folders)
if (-not $NoSkills) {
  foreach ($d in @("$HOME\.claude\skills", "$HOME\.grok\skills")) {
    $t = Join-Path $d "qemu-harness"
    New-Item -ItemType Directory -Force $t | Out-Null
    Copy-Item "$Root\skill\SKILL.md" "$t\SKILL.md" -Force
    Ok "skill -> $t"
  }
}

# --- smoke test
if ($Smoke) {
  $tmp = Join-Path $env:TEMP ("qh-smoke-" + [guid]::NewGuid().ToString("N").Substring(0,6))
  New-Item -ItemType Directory $tmp | Out-Null; Push-Location $tmp
  try {
    '{"kernel":"alpine:' + $archs[0] + '","fwd":[]}' | Set-Content qh.json -Encoding ascii
    & $py.Source "$Root\qh.py" up --timeout 120; if ($LASTEXITCODE) { Fail "smoke: up failed" }
    & $py.Source "$Root\qh.py" exec "uname -a"
    & $py.Source "$Root\qh.py" down | Out-Null
    Ok "smoke test passed"
  } finally { Pop-Location; Start-Sleep 1; Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
Write-Host "`nDone. Try:  mkdir lab; cd lab; qh up --kernel alpine:$($archs[0]); qh exec 'uname -a'; qh down" -ForegroundColor Cyan
