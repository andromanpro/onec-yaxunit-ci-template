# Wrapper для прогона YAxUnit smoke-модуля из CI через vanessa-runner (vrunner)
# Запускается Gitea Actions Windows runner'ом
#
# vrunner — production-grade CLI для CI 1С от vanessa-opensource (250+ ⭐).
# Решает все грабли запуска 1С CLI: кодировка args, /DisableStartupDialogs,
# /N+/P-форматирование, пути с пробелами.
#
# Логика:
#   1. Pre-flight — проверка что 1cv8 не открыт (защита от чужой работы)
#   2. Подготовка YAxUnit конфига с фильтром на модуль
#   3. vrunner run --command "RunUnitTests=..." — запуск 1С
#   4. Парсинг JUnit XML отчёта
#   5. Exit 0 (зелёный) / 1 (красный)

param(
  # Путь до файловой ИБ. Замените на свой или передайте через -BasePath
  [Parameter(Mandatory = $true)]
  [string]$BasePath,
  # ASCII-пользователь для CI — обходит Win32 charset border (см. README Грабля 2)
  [string]$BaseUser = "ci-user",
  # Пароль — НЕ хардкодить. Передайте через -BasePass или env YAXUNIT_BASE_PASS
  [string]$BasePass = $env:YAXUNIT_BASE_PASS,
  [string]$V8Version = "8.3.27.1989",
  # vrunner — путь до vrunner.bat, обычно установленного через ovm/opm.
  # См. README Pre-requisites
  [string]$VrunnerExe = "vrunner.bat",
  # Имя smoke-модуля для прогона. Замените на свой или передайте через -ModuleName
  [string]$ModuleName = "Тесты_СмокYAXUnit",
  [string[]]$ModuleNames = @(),
  # JUnit XML отчёт пишется в этот каталог (default — рядом с workflow workspace)
  [string]$ReportDir = "",
  [int]$TimeoutSec = 600
)

# Default ReportDir — repo-local, согласован с upload-to-allure.py
if (-not $ReportDir) {
  $ReportDir = Join-Path $PSScriptRoot "ci-reports"
}

# Резолвинг списка модулей
if ($ModuleNames.Count -eq 0) {
  $ModuleNames = @($ModuleName)
}

$ErrorActionPreference = 'Stop'

try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

# ============================================================
# 1. Pre-flight
# ============================================================
Write-Host "[1/5] Pre-flight: проверка активных Enterprise-сессий 1С" -ForegroundColor Cyan
# Блокируем только ENTERPRISE-сессии (они держат lock на test extension).
# DESIGNER/Конфигуратор не мешает прогону YAxUnit — можно работать параллельно.
$blocking = Get-CimInstance Win32_Process -Filter "Name='1cv8.exe' OR Name='1cv8c.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match '(?i)\bENTERPRISE\b' }
if ($blocking) {
  Write-Host "  FAIL: открыта ENTERPRISE-сессия 1С — не убиваем чужие:" -ForegroundColor Red
  $blocking | ForEach-Object { Write-Host "    PID $($_.ProcessId) $($_.Name) $(($_.CommandLine -replace '/P[`"][^`"]+[`"]', '/P***'))" }
  exit 2
}
if (-not $BasePass) {
  Write-Host "  FAIL: пароль не задан. Установите env YAXUNIT_BASE_PASS или передайте -BasePass" -ForegroundColor Red
  exit 2
}
if (-not (Test-Path $VrunnerExe)) {
  Write-Host "  FAIL: vrunner не найден по пути $VrunnerExe" -ForegroundColor Red
  Write-Host "  Установите: $VrunnerExe-каталог/opm.bat install vanessa-runner" -ForegroundColor Yellow
  exit 2
}
# vrunner.bat внутри делает `call oscript ...` — oscript.exe должен резолвиться через PATH.
# Под LocalSystem (Gitea runner-as-service) user-scoped PATH не наследуется,
# поэтому добавляем папку ovm/bin в $env:Path для текущего процесса.
$ovmBin = Split-Path -Parent $VrunnerExe
if ($env:Path -notlike "*$ovmBin*") {
  $env:Path = "$ovmBin;$env:Path"
  Write-Host "  PATH расширен: $ovmBin"
}
Write-Host "  OK — 1С не запущен, vrunner найден, пароль задан"

# ============================================================
# 2. Конфиг YAxUnit
# ============================================================
Write-Host "[2/5] Подготовка YAxUnit-конфига" -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
$Timestamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$ConfigPath = Join-Path $ReportDir "yaxunit-smoke-$Timestamp.json"
$ReportPath = Join-Path $ReportDir "yaxunit-smoke-$Timestamp.xml"
$LogPath    = Join-Path $ReportDir "yaxunit-smoke-$Timestamp.log"

$cfg = [ordered]@{
  filter = @{ modules = $ModuleNames }
  reportFormat = 'jUnit'
  reportPath = $ReportPath
  closeAfterTests = $true
  showReport = $false
  logging = @{ file = $LogPath; console = $false; level = 'info' }
}
$cfg | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigPath -Encoding UTF8
Write-Host "  Config: $ConfigPath"
Write-Host "  Report: $ReportPath"
Write-Host "  Modules: $($ModuleNames -join ', ')"

# ============================================================
# 3. Запуск через vrunner
# ============================================================
Write-Host "[3/5] Запуск vrunner run (timeout $TimeoutSec sec)" -ForegroundColor Cyan

# PS 5.1: ProcessStartInfo.ArgumentList отсутствует — используем Arguments (string).
# Каждый аргумент со значением закавычиваем (на случай пробелов и кириллицы).
$vrunnerArgs = @(
  'run',
  '--command', "`"RunUnitTests=$ConfigPath`"",
  '--ibconnection', "`"/F$BasePath`"",
  '--db-user', "`"$BaseUser`"",
  '--db-pwd', "`"$BasePass`"",
  '--v8version', $V8Version
)
$argsStr = $vrunnerArgs -join ' '

$displayCmd = $argsStr -replace [regex]::Escape($BasePass), '***'
Write-Host "  cmd: $VrunnerExe $displayCmd"

$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $VrunnerExe
$psi.Arguments = $argsStr
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

$proc = [System.Diagnostics.Process]::Start($psi)
$stdoutTask = $proc.StandardOutput.ReadToEndAsync()
$stderrTask = $proc.StandardError.ReadToEndAsync()
if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
  Write-Host "  TIMEOUT — vrunner не завершился, kill" -ForegroundColor Red
  try { $proc.Kill($true) } catch { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
  exit 3
}
$exitCode = $proc.ExitCode
$stdout = $stdoutTask.Result
$stderr = $stderrTask.Result
Write-Host "  vrunner exit code: $exitCode"
if ($stdout) { Write-Host "  --- stdout ---"; $stdout.Split("`n") | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" } }
if ($stderr) { Write-Host "  --- stderr ---"; $stderr.Split("`n") | Select-Object -First 30 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow } }

# ============================================================
# 4. Парсинг JUnit XML
# ============================================================
Write-Host "[4/5] Парсинг отчёта" -ForegroundColor Cyan

if (-not (Test-Path $ReportPath)) {
  Write-Host "  FAIL: JUnit отчёт не найден по пути $ReportPath" -ForegroundColor Red
  if (Test-Path $LogPath) {
    Write-Host "  Лог YAxUnit (последние 30 строк):"
    Get-Content $LogPath -Tail 30 | ForEach-Object { Write-Host "    $_" }
  }
  exit 4
}
Write-Host "  Отчёт: $ReportPath"

# PS 5.1: Get-Content без -Encoding читает как cp1251 → mojibake на UTF-8 XML.
# Используем -Raw -Encoding UTF8 чтобы кириллица в названиях тестов читалась корректно.
[xml]$xml = Get-Content $ReportPath -Raw -Encoding UTF8
$suites = $xml.testsuites.testsuite
if (-not $suites) { $suites = @($xml.testsuite) }

$total = 0; $failed = 0; $errored = 0; $skipped = 0
foreach ($s in $suites) {
  $total += [int]$s.tests
  $failed += [int]$s.failures
  $errored += [int]$s.errors
  $skipped += [int]$s.skipped
}
$passed = $total - $failed - $errored - $skipped

Write-Host ""
Write-Host "  Tests: $total | Passed: $passed | Failed: $failed | Errored: $errored | Skipped: $skipped"
foreach ($s in $suites) {
  Write-Host "    Suite '$($s.name)': tests=$($s.tests) failures=$($s.failures) errors=$($s.errors)"
  if ([int]$s.failures -gt 0 -or [int]$s.errors -gt 0) {
    foreach ($tc in $s.testcase) {
      if ($tc.failure -or $tc.error) {
        $msg = if ($tc.failure) { $tc.failure.'#text' } else { $tc.error.'#text' }
        Write-Host "      X $($tc.name): $msg" -ForegroundColor Red
      }
    }
  }
}

# ============================================================
# 5. Exit code
# ============================================================
Write-Host "[5/5] Результат" -ForegroundColor Cyan
if ($failed -gt 0 -or $errored -gt 0) {
  Write-Host "  RED: $($failed + $errored) тестов упало" -ForegroundColor Red
  exit 1
}
if ($total -eq 0) {
  Write-Host "  AMBER: 0 тестов выполнено" -ForegroundColor Yellow
  exit 5
}
Write-Host "  GREEN: $passed/$total passed" -ForegroundColor Green
exit 0
