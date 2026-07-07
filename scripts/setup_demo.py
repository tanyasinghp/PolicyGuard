"""One-command environment verification and demo preparation.

Checks the toolchain, compiles the policy, runs the offline test suite,
and generates the episode suite. Prints exactly what remains manual
(API key, eval run, streamlit).

Usage: python scripts/setup_demo.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(label: str, args: list[str]) -> bool:
    """Run one step, streaming output; return success."""
    print(f"\n=== {label} ===")
    completed = subprocess.run(args, cwd=ROOT)  # noqa: S603
    ok = completed.returncode == 0
    print(f"--- {label}: {'OK' if ok else 'FAILED'}")
    return ok


def main() -> int:
    steps = [
        ("compile policy graph",
         [sys.executable, "-m", "policy.compile_graph",
          "policy/rules_sg_mas626.yaml"]),
        ("offline test suite",
         [sys.executable, "-m", "pytest", "guard/", "agent/", "eval/", "-q"]),
        ("generate episode suite",
         [sys.executable, "scripts/generate_episodes.py"]),
    ]
    if not all(run(label, args) for label, args in steps):
        print("\nSetup incomplete -- fix the failing step above.")
        return 1
    env_file = ROOT / ".env"
    print("\nAll offline steps passed. Remaining manual steps:")
    if not env_file.exists():
        print("  1. cp .env.example .env  # and set ANTHROPIC_API_KEY")
    print("  2. python scripts/m3_smoke.py                  # one live episode")
    print("  3. python -m eval.run_eval --conditions self_policing "
          "guarded_oracle --limit 3")
    print("  4. streamlit run demo/demo.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())