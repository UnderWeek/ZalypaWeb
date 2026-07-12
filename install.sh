#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_ROOT"
PYTHON=${PYTHON:-python3}

"$PYTHON" -c "import sys; assert sys.version_info >= (3, 12), 'Auralis requires Python 3.12+'"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip setuptools wheel
REQUIREMENTS=requirements.txt
if [ "${1:-}" = "--dev" ]; then
  REQUIREMENTS=requirements-dev.txt
fi
.venv/bin/python -m pip install -r "$REQUIREMENTS"
.venv/bin/python -m pip install -e .
printf '%s\n' 'Auralis установлен. Запуск: ./run.sh'

