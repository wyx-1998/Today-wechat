#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TASK_APP="$TASK_ROOT/apps/desktop/src-tauri/target/debug/bundle/macos/wechat-today.app"
if [ ! -d "$TASK_APP" ]; then
  echo "尚未构建应用，请先运行 bash scripts/build_desktop.sh。" >&2
  exit 1
fi
open "$TASK_APP"
