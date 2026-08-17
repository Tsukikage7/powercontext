#!/bin/sh
set -eu

python - <<'PY'
from pathlib import Path

client = Path("/workspace/client.py").read_text(encoding="utf-8")
marker = Path("/workspace/generated-client-v3.txt").read_text(encoding="utf-8")
raise SystemExit(0 if 'FEATURE_FLAG = "stable-v3"' in client and marker == "GENERATED_CLIENT_V3\n" else 1)
PY
echo 1 > /logs/verifier/reward.txt
