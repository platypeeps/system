"""Isolated internal entry point; dashboard.sh selects the installed interpreter."""

import runpy
import sys
from pathlib import Path

# -I ignores PYTHONPATH and the caller's working directory. Only this dashboard
# package is added; the adjacent local-sd-db source must never shadow the build.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
module = sys.argv.pop(1)
if module not in ("server", "runtime"):
    raise SystemExit("unknown dashboard module")
runpy.run_module(f"sd_dashboard.{module}", run_name="__main__")
