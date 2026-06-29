#!/usr/bin/env python3
"""Silent-failure regression gate.

Runs in CI to block regressions of the 0.2.7 silent-failure audit:

  1. **Hard bans** (always fail) — anti-patterns the audit eliminated:
     - bare ``except:``
     - ``except Exception: pass``
     - ``except Exception: return None`` (in user-handler-path modules)
  2. **Annotation requirement on new additions** (diff-aware) —
     ``_notify(...)`` and ``contextlib.suppress(...)`` introduced in
     a PR must carry an ``audited(0.2.X)`` annotation within ±5 lines.
     Existing pre-audit occurrences are not flagged.

See ``docs/contributor/silent-failure-audit.md`` for the audit
baseline this gate guards.

Exit codes
----------
0  Gate passes.
1  One or more findings; details printed to stderr.

Usage
-----
::

    python scripts/silent_failure_gate.py
    BASE_REF=main python scripts/silent_failure_gate.py

When ``BASE_REF`` is unset, the diff-aware check is skipped (only
hard bans are enforced). CI sets ``BASE_REF`` to the PR's base ref.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Gate scope is ``src/dimfort/lsp/`` — the audit's primary surface. Core
# modules (``ts_checker``, ``units``, ``rewrite``, …) are carry-forward
# for 0.2.8 and intentionally out of scope here; they hold legitimate
# silent-fallback patterns (e.g. unit parse failures returning None as
# a documented contract) that pre-date the audit's classification
# discipline and would need their own focused review.
SCAN_DIR = ROOT / "src" / "dimfort" / "lsp"
ANNOTATION = re.compile(r"audited\(0\.2\.\d+\)")
WINDOW = 5  # lines of context to search for the annotation


# Hard-banned patterns. (regex, description). Matched line-by-line.
HARD_BANS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^\s*except\s*:\s*(#.*)?$"),
        "bare `except:` — name the exception class, even if catching all",
    ),
]

# Multi-line hard bans matched against the full file text.
HARD_BANS_MULTILINE: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"except\s+Exception\s*:\s*\n\s*pass\b"),
        "`except Exception:` followed immediately by `pass` — annotate "
        "silent-OK or surface the error",
    ),
    (
        re.compile(r"except\s+Exception\s*:\s*\n\s*return\s+None\b"),
        "`except Exception:` followed immediately by `return None` — annotate "
        "silent-OK with rationale or surface via log.warning",
    ),
]

# Patterns that require an `audited(0.2.X)` annotation within ±5 lines
# when newly introduced (diff-aware).
ANNOTATION_REQUIRED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b_notify\s*\("), "_notify() call"),
    (re.compile(r"contextlib\.suppress\b"), "contextlib.suppress"),
]


def hard_ban_findings() -> list[tuple[Path, int, str, str]]:
    out: list[tuple[Path, int, str, str]] = []
    for path in SCAN_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for i, line in enumerate(lines, start=1):
            for pat, desc in HARD_BANS:
                if pat.search(line):
                    out.append((path, i, desc, line.strip()))
        for pat, desc in HARD_BANS_MULTILINE:
            for m in pat.finditer(text):
                lineno = text.count("\n", 0, m.start()) + 1
                out.append((path, lineno, desc, m.group(0).strip()))
    return out


def annotation_findings_in_diff(base_ref: str) -> list[tuple[Path, int, str, str]]:
    """Find new occurrences of ANNOTATION_REQUIRED patterns lacking annotation.

    Uses ``git diff`` against ``base_ref`` to find added lines. For each
    added line matching a tracked pattern, checks whether an
    ``audited(0.2.X)`` annotation appears within ±5 lines in the current
    file.
    """
    cmd = [
        "git",
        "diff",
        "--unified=0",
        "--no-color",
        f"{base_ref}...HEAD",
        "--",
        "src/dimfort/**/*.py",
    ]
    try:
        diff = subprocess.check_output(cmd, cwd=ROOT, text=True)
    except subprocess.CalledProcessError as exc:
        print(f"silent_failure_gate: git diff failed ({exc})", file=sys.stderr)
        sys.exit(2)

    # Parse unified diff: identify added lines per file and their new-file lineno.
    findings: list[tuple[Path, int, str, str]] = []
    current_path: Path | None = None
    current_line = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            current_path = ROOT / raw[6:]
        elif raw.startswith("@@"):
            # @@ -old_start,old_count +new_start,new_count @@
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", raw)
            if m:
                current_line = int(m.group(1)) - 1
        elif raw.startswith("+") and not raw.startswith("+++"):
            current_line += 1
            line = raw[1:]
            for pat, desc in ANNOTATION_REQUIRED:
                if pat.search(line):
                    if current_path is None or not current_path.exists():
                        continue
                    text = current_path.read_text(encoding="utf-8")
                    lines = text.splitlines()
                    lo = max(0, current_line - 1 - WINDOW)
                    hi = min(len(lines), current_line + WINDOW)
                    window = "\n".join(lines[lo:hi])
                    if not ANNOTATION.search(window):
                        findings.append((current_path, current_line, desc, line.strip()))
        elif raw.startswith(" "):
            current_line += 1
        # '-' lines and other diff metadata don't advance the new-file line.

    return findings


def main() -> int:
    failures = hard_ban_findings()
    base_ref = os.environ.get("BASE_REF")
    if base_ref:
        failures.extend(annotation_findings_in_diff(base_ref))

    if not failures:
        print("silent_failure_gate: OK")
        return 0

    print(
        "silent_failure_gate: FAILED — the following patterns regress the "
        "0.2.7 silent-failure audit:",
        file=sys.stderr,
    )
    for path, lineno, desc, content in failures:
        rel = path.relative_to(ROOT)
        truncated = content if len(content) <= 100 else content[:97] + "..."
        print(f"  {rel}:{lineno}  [{desc}]", file=sys.stderr)
        print(f"    {truncated}", file=sys.stderr)
    print(
        "\nFix: classify each finding (silent-OK with rationale, or fix "
        "the silent failure with log.warning / toast surfacing), then "
        "add `audited(0.2.X): <classification> — <reason>` in the catch "
        "block / nearby. See docs/contributor/silent-failure-audit.md "
        "for the audit baseline and resolution templates.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
