# Запуск бота на этом компьютере (его вызывает планировщик Windows в 9:00 и 18:00).
# Пишет лог в logs\, затем отправляет память бота (state\) в GitHub как резервную копию.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root "logs") | Out-Null
$log = Join-Path $root ("logs\" + (Get-Date -Format "yyyy-MM-dd_HH-mm") + ".log")
$env:PYTHONIOENCODING = "utf-8"

$python = "C:\Program Files\Python312\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m agent.main *>&1 | Out-File -FilePath $log -Encoding utf8

# резервная копия памяти в GitHub (если не получилось — не страшно, память лежит и здесь)
git add state/ *>> $log
git diff --cached --quiet
if ($LASTEXITCODE -ne 0) {
    git -c user.name="opportunity-agent" -c user.email="actions@users.noreply.github.com" commit -q -m ("state: pc run " + (Get-Date -Format "yyyy-MM-dd HH:mm")) *>> $log
    git push -q *>> $log
}

# старые логи старше 30 дней удаляем
Get-ChildItem (Join-Path $root "logs") -Filter *.log | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | Remove-Item -Force
