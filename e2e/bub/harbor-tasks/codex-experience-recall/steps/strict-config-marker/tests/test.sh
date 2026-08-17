#!/bin/sh
set -eu

python - <<'PY'
import json
from pathlib import Path

actual = json.loads(Path("/workspace/config.json").read_text(encoding="utf-8"))
expected = {"mode": "strict", "marker": "POWERCONTEXT_STRICT_V2"}
raise SystemExit(0 if actual == expected else 1)
PY
echo 1 > /logs/verifier/reward.txt
