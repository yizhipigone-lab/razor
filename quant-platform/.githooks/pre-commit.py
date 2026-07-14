#!/usr/bin/env python3
"""pre-commit gate — Windows + bash 兼容,零依赖。"""
import re
import subprocess
import sys
from pathlib import Path

MAX_FILE_LINES = 800
FORBIDDEN_PATTERNS = [
    (re.compile(r"https://open\.feishu\.cn/open-apis/bots/v2/hook/[a-f0-9-]{20,}"), "feishu webhook URL"),
    (re.compile(r"wh-[a-f0-9]{20,}"), "Microsoft Teams webhook"),
    (re.compile(r"hooks\.slack\.com/services/T[a-zA-Z0-9_]+/B[a-zA-Z0-9_]+/[a-zA-Z0-9_]+"), "Slack webhook"),
    (re.compile(r"AKID[A-Z0-9]{20,}"), "Aliyun AccessKey"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "Google API Key"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI/Anthropic API Key"),
]
FORBIDDEN_PATHS = ["docs/_research/", "docs/_drafts/", ".claude/"]


def find_git_root_with_staged():
    """扫常见位置找包含 staged 文件的 git 仓库根(子仓库场景)。"""
    candidates = [Path.cwd(), Path.cwd().parent, Path.cwd().parent.parent]
    for c in candidates:
        if not (c / ".git").exists():
            continue
        r = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMRT"],
            cwd=str(c), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and r.stdout.strip():
            return c, r.stdout.strip().splitlines()
    return None, []


def main():
    git_root, raw_files = find_git_root_with_staged()
    if not raw_files:
        return 0

    files = []
    for f in raw_files:
        if not f:
            continue
        full_path = git_root / f
        try:
            content = full_path.read_bytes()
            text = content.decode("utf-8", errors="replace")
            line_count = text.count("\n") + 1
        except (OSError, UnicodeError):
            line_count = 0
        files.append((f, line_count))

    errors = []
    for f, lc in files:
        if lc > MAX_FILE_LINES and f.endswith((".py", ".md", ".json", ".yaml", ".yml")):
            errors.append(f"[Gate 1] {f}: {lc} 行 > {MAX_FILE_LINES} 行")
    for f, _ in files:
        if not f.endswith((".py", ".json", ".md", ".yaml", ".yml", ".env", ".sh", ".bat")):
            continue
        try:
            content = (git_root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern, name in FORBIDDEN_PATTERNS:
            if pattern.search(content):
                errors.append(f"[Gate 2] {f}: {name}")
    for f, _ in files:
        for bad in FORBIDDEN_PATHS:
            if f.startswith(bad) or f"/{bad}" in f or f.endswith(bad.rstrip("/")):
                errors.append(f"[Gate 3] {f}: 禁止路径({bad})")
                break

    if errors:
        sys.stderr.write("=" * 70 + "\n")
        sys.stderr.write("[PRE-COMMIT FAIL]\n")
        for e in errors:
            sys.stderr.write(f"  {e}\n")
        sys.stderr.write("跳过: git commit --no-verify\n")
        sys.stderr.write("=" * 70 + "\n")
        sys.stderr.flush()
        return 1

    sys.stderr.write(f"[PRE-COMMIT OK] {len(files)} 文件通过\n")
    sys.stderr.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
