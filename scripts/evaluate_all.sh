#!/usr/bin/env bash
# 在全部评估基准上回放全部样例集，产出报告。
# 任何一个样例集未达标即非零退出——这就是 CI 里的门禁，本地也能直接跑。
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
APROBE="${APROBE:-.venv/bin/aprobe}"
REPEAT="${REPEAT:-2}"
REPORTS="${REPORTS:-reports}"

mkdir -p "$REPORTS"

pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT

start_baseline() { # name spec_service scenario port
  "$PYTHON" "mock/$1" --scenario "$2" --port "$3" >"$REPORTS/baseline-$1-$2.log" 2>&1 &
  pids+=("$!")
}

start_baseline mock_service.py conformant 18080
start_baseline mock_service.py violating 18081
start_baseline edge_service.py conformant 18082
start_baseline edge_service.py violating 18083

# 等基准起来；主动探测而不是 sleep 猜时间
for port in 18080 18081 18082 18083; do
  for _ in $(seq 1 50); do
    if "$PYTHON" - "$port" <<'PROBE' 2>/dev/null
import json, sys, urllib.request
with urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/__scenario", timeout=0.5) as response:
    assert json.load(response)["scenario"]
PROBE
    then break; fi
    sleep 0.1
  done
done

failed=0
suites=(
  "eval/petstore-conformant.yaml:18080"
  "eval/petstore-violating.yaml:18081"
  "eval/edgecases-conformant.yaml:18082"
  "eval/edgecases-violating.yaml:18083"
)
for entry in "${suites[@]}"; do
  suite="${entry%%:*}"; port="${entry##*:}"
  name="$(basename "$suite" .yaml)"
  echo "=== $name ==="
  if ! "$APROBE" evaluate \
      --config aprobe.yaml \
      --suite "$suite" \
      --target "http://127.0.0.1:$port" \
      --repeat "$REPEAT" \
      --json "$REPORTS/eval-$name.json"; then
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "评估未达标：见 $REPORTS/eval-*.json" >&2
  exit 1
fi
echo "全部样例集达标。"
