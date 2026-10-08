"""Instruction caps (sd:3005): the repo CLAUDE.md stays at or under 150 lines, the shared agent block at or under 200.

Stdlib only, so it runs in the `tests/ci-native.sh` preflight before the venv.
`python3 tests/test_instruction_caps.py` from the repository root is the whole invocation.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAPS = {"CLAUDE.md": 150, "local-agent-prompt/prompt/shared.md": 200}


class InstructionCaps(unittest.TestCase):
    def test_each_instruction_file_is_within_its_cap(self):
        for name, cap in CAPS.items():
            count = len((ROOT / name).read_text(encoding="utf-8").splitlines())
            with self.subTest(file=name):
                self.assertLessEqual(count, cap, f"{name} has {count} lines; the cap is {cap}")


if __name__ == "__main__":
    unittest.main()
