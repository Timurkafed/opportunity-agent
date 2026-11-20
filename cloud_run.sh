#!/usr/bin/env bash
# One scheduled run in the Claude Code cloud: newest code and memory, digest, memory back to GitHub.
# The last line is always "RESULT: ..." so the scheduled session only has to report it.
cd "$(dirname "$0")" || exit 1
export OPP_RUNNER=cloud PYTHONIOENCODING=utf-8

git fetch -q origin main && git checkout -q -B main FETCH_HEAD || echo "warning: could not update to the newest main"

log=$(mktemp)
timeout 540 python3 -u -m agent.main 2>&1 | tee "$log"; status=${PIPESTATUS[0]}
if [ $status -eq 124 ]; then
  python3 -c "from agent import main as m, store; m.notify_failure(store.load('meta.json', {}), 'запуск не уложился в 9 минут и остановлен. Что не успел, пришлю в следующий раз.')"
fi
python3 -m agent.gitstate cloud; save=$?

if [ $status -eq 124 ]; then
  echo "RESULT: FAIL (timed out after 9 min)"
elif [ $status -ne 0 ]; then
  echo "RESULT: FAIL (exit $status)"
elif [ $save -ne 0 ]; then
  echo "RESULT: FAIL (memory not saved)"
elif grep -q "^SKIP:" "$log"; then
  echo "RESULT: SKIP (another run was less than 4 hours ago)"
else
  echo "RESULT: OK $(grep '^Done\.' "$log" | tail -1)"
fi
exit $(( status || save ))
