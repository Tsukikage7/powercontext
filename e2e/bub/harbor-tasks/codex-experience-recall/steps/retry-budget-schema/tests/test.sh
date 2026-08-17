#!/bin/sh
set -eu

python - <<'PY'
import json
from pathlib import Path

actual = json.loads(Path("/workspace/retry.json").read_text(encoding="utf-8"))
expected = {"retry_budget_ms": 3000}
raise SystemExit(0 if actual == expected else 1)
PY
echo 1 > /logs/verifier/reward.txt
