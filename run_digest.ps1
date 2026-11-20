# Запуск бота на этом компьютере (его вызывает Планировщик Windows в 9:00 и 18:00).
# Теперь ноутбук ЗАПАСНОЙ: главный запуск идёт из облака Claude в 9:07 и 18:07.
# Поэтому ждём 30 минут, берём свежий код и память из GitHub, и бот сам пропускает запуск,
# если облако уже отработало (меньше 4 часов назад). Если облако не сработало, дайджест пришлёт ноутбук.
# Ручной запуск без ожидания:  $env:OPP_NO_WAIT=1; .\run_digest.ps1
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$log = Join-Path $root ("logs\" + (Get-Date -Format "yyyy-MM-dd_HH-mm") + ".log")
$env:PYTHONIOENCODING = "utf-8"
$env:OPP_RUNNER = "pc"

$python = "C:\Program Files\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

if (-not $env:OPP_NO_WAIT) {
    "waiting 30 min for the cloud run..." | Out-File -FilePath $log -Encoding utf8
    Start-Sleep -Seconds 1800
}

# свежий код (если не получилось, бот всё равно возьмёт свежую память сам)
git pull --ff-only -q 2>&1 | Out-File -FilePath $log -Encoding utf8 -Append

& $python -m agent.main *>&1 | Out-File -FilePath $log -Encoding utf8 -Append

# память бота в GitHub: облако и ноутбук должны видеть одну и ту же
& $python -m agent.gitstate pc *>&1 | Out-File -FilePath $log -Encoding utf8 -Append

# старые логи старше 30 дней удаляем
Get-ChildItem (Join-Path $root "logs") -Filter *.log | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
