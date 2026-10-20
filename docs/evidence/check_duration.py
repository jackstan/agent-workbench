"""Independent counterexample reproducer; does not modify the sandbox test suite.

Run: python check_duration.py /path/to/sandbox-checkout
Expected: exit 1 on the seeded capped commit, exit 0 on the repaired commit.
"""
from pathlib import Path
import subprocess
import sys

repository = Path(sys.argv[1]).resolve()
failures = 0
for value, expected in (("180m", 180), ("181m", 181), ("4h", 240), ("2h 2h", 240)):
    result = subprocess.run([sys.executable, "-B", "-m", "src.duration", value],
                            cwd=repository, capture_output=True, text=True, timeout=5)
    passed = result.returncode == 0 and result.stdout.strip() == str(expected)
    print(f"{value!r}: expected={expected}, actual={result.stdout.strip()!r}, pass={passed}")
    failures += not passed
raise SystemExit(1 if failures else 0)
