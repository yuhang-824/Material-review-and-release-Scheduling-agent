"""Run stdlib tests, persist their real stdout/stderr and exit status."""
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONUTF8": "1"}
result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                        cwd=root, env=env, text=True, encoding="utf-8", capture_output=True)
output = (f"UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
          f"Command: python -m unittest discover -s tests -v\nExit code: {result.returncode}\n\n"
          + result.stdout + result.stderr)
path = root / "examples" / "test-results.txt"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(output, encoding="utf-8")
print(output)
raise SystemExit(result.returncode)
