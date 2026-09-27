"""Isolated service bootstrap: add only this service, use installed sd_db."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sd_runner.cli import main

raise SystemExit(main())
