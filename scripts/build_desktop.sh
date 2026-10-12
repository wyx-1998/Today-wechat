#!/bin/bash
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$TASK_ROOT"
export CARGO_HOME="$TASK_ROOT/.toolchains/cargo"
export RUSTUP_HOME="$TASK_ROOT/.toolchains/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
if [ "$(uname -m)" != arm64 ]; then
  echo "当前构建脚本仅验证 Apple Silicon；其他架构请先单独验证。" >&2
  exit 1
fi
if [ ! -x "$CARGO_HOME/bin/cargo" ]; then
  mkdir -p "$TASK_ROOT/.toolchains"
  curl --fail --location --proto '=https' --tlsv1.2 https://static.rust-lang.org/rustup/dist/aarch64-apple-darwin/rustup-init -o "$TASK_ROOT/.toolchains/rustup-init"
  chmod 700 "$TASK_ROOT/.toolchains/rustup-init"
  "$TASK_ROOT/.toolchains/rustup-init" -y --profile minimal --default-toolchain stable --no-modify-path
fi
if [ ! -x "$TASK_ROOT/.venv-build/bin/python" ]; then
  python3 -m venv "$TASK_ROOT/.venv-build"
fi
"$TASK_ROOT/.venv-build/bin/python" -m pip install -r "$TASK_ROOT/requirements-build.txt"
"$TASK_ROOT/.venv-build/bin/python" -m PyInstaller --noconfirm --onefile --name reader-aarch64-apple-darwin --distpath "$TASK_ROOT/apps/desktop/src-tauri/binaries" --workpath "$TASK_ROOT/.build/pyinstaller" --specpath "$TASK_ROOT/.build" --paths "$TASK_ROOT" --hidden-import zstandard --hidden-import Crypto.Cipher.AES "$TASK_ROOT/backend/desktop_entry.py"
cd "$TASK_ROOT/apps/desktop"
npm ci
npm run tauri -- icon src-tauri/icons/source.svg
npm run tauri -- build --debug
