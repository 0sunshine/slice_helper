#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  PYTHON="$PROJECT_ROOT/.venv/bin/python"
else
  PYTHON="python3"
fi

cd "$PROJECT_ROOT"
exec "$PYTHON" -m slice_helper
