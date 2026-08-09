#!/usr/bin/env python3
"""Run each backend test file in its own pytest process with a hard timeout.

Prints one line per file: PASS / FAIL / TIMEOUT / ERROR with counts, so a single
hanging test can't block the whole suite and we can see aggregate health.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = str(ROOT / ".venv" / "bin" / "python")
TIMEOUT = 90  # seconds per file

files = sorted(
    p for p in ROOT.glob("tests/**/*.py")
    if p.name.startswith("test_") and p.name != "test_smoke.py"
)
files = [ROOT / "tests" / "test_smoke.py"] + files

total_pass = total_fail = 0
hung = []
failed_files = []

for f in files:
    rel = f.relative_to(ROOT)
    try:
        proc = subprocess.run(
            [PY, "-m", "pytest", str(rel), "-p", "no:cacheprovider", "-o", "addopts=", "-q", "--tb=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        hung.append(str(rel))
        print(f"TIMEOUT  {rel}")
        continue

    out = proc.stdout + proc.stderr
    # Parse the last summary line.
    summary = ""
    for line in reversed(out.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary = line.strip()
            break
    tag = "PASS" if proc.returncode == 0 else "FAIL"
    if proc.returncode != 0:
        failed_files.append(str(rel))
    print(f"{tag:8} {rel}  |  {summary}")

print("\n=== SUMMARY ===")
print(f"files: {len(files)}  failed_files: {len(failed_files)}  hung: {len(hung)}")
if hung:
    print("HUNG:", hung)
if failed_files:
    print("FAILED:", failed_files)
