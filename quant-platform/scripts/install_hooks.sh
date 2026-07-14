#!/usr/bin/env bash
# 一键装 pre-commit hook(Windows + Linux 通用)
# 用法: bash scripts/install_hooks.sh
GIT_DIR=$(git rev-parse --git-dir)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="$SCRIPT_DIR/.githooks"

if [ ! -d "$GIT_DIR" ]; then
  echo "[FAIL] 当前目录不是 git 仓库"
  exit 1
fi

# 优先用项目自己的 .githooks,回退到全局 hooks
if [ -d "$HOOKS_DIR" ]; then
  git config core.hooksPath "$HOOKS_DIR"
  echo "[OK] core.hooksPath → $HOOKS_DIR"
else
  echo "[FAIL] 未找到 .githooks/"
  exit 1
fi
