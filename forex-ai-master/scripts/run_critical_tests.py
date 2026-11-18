#!/usr/bin/env python3
"""Run the critical test suite and write the report required to enable LIVE mode.

The report contains the source-code hash: any later code change invalidates it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.security.live_guard import source_hash  # noqa: E402


def main() -> int:
    out = ROOT / "var" / "critical_tests.json"
    junit = ROOT / "var" / "critical_junit.xml"
    out.parent.mkdir(exist_ok=True)
    started = datetime.now(UTC)
    code = subprocess.call([sys.executable, "-m", "pytest", "-m", "critical", "-q", f"--junitxml={junit}"], cwd=ROOT)
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    assert suite is not None
    tests, failures = int(suite.get("tests", 0)), int(suite.get("failures", 0))
    errors, skipped = int(suite.get("errors", 0)), int(suite.get("skipped", 0))
    report = {"started_at": started.isoformat(), "finished_at": datetime.now(UTC).isoformat(),
              "passed": tests - failures - errors - skipped, "failed": failures, "errors": errors,
              "skipped": skipped, "exit_code": code, "source_hash": source_hash()}
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if code == 0 and failures == 0 and errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
