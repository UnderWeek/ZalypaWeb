#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$PROJECT_ROOT/.venv/bin/python" ]; then
  printf '%s\n' 'Виртуальное окружение не найдено. Сначала выполните ./install.sh' >&2
  exit 1
fi
cd "$PROJECT_ROOT"
exec .venv/bin/python -m browser.main "$@"

