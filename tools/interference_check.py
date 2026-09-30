"""Record and compare the state of a neighbouring repository.

driver-core is built in its own repository, beside a separately developed
project that has work in flight. The operator's requirement for that project
is that this work must not affect it. "Must not affect it" is not something
to be promised in a document -- it is something to be measured, on every
segment boundary, by a command whose failure is visible.

So this tool snapshots four things about the neighbouring repository and
compares them:

1. ``HEAD`` -- the checked-out commit.
2. ``git worktree list`` -- any new worktree means this project has been
   operating inside the other one.
3. ``git status --porcelain`` -- any output at all means a file was added,
   edited, or deleted, including an untracked one.
4. State files -- checksums of the ledger and any other shared state, so a
   stray run that appended a record is caught even though it changed no
   code.

Usage::

    python tools/interference_check.py --repo <path> --record baseline.json
    python tools/interference_check.py --repo <path> --record baseline.json --check

Exit code 0 means no interference, 1 means interference was detected and
the differences are printed.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

#: State files that matter. A driver run that appends to another project's
#: ledger changes no code, so the code checks alone would miss it -- which is
#: exactly the kind of quiet damage that is hardest to notice later.
STATE_PATTERNS = ("*.jsonl", "*.db", "*.ledger")


def _git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, shell=False, check=False)
    if result.returncode != 0:
        return f"<git failed: {result.stderr.strip()[:200]}>"
    return result.stdout.strip()


def snapshot(repo):
    """The four-part snapshot of ``repo``."""
    repo = Path(repo)
    return {
        "repo": str(repo),
        "head": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "worktrees": sorted(
            line for line in _git(repo, "worktree", "list").splitlines() if line),
        "status": sorted(
            line for line in _git(repo, "status", "--porcelain").splitlines()
            if line),
        "state_files": _state_digests(repo),
    }


def _state_digests(repo, limit=200):
    """Checksums of shared state files, searched outside the git objects."""
    digests = {}
    for pattern in STATE_PATTERNS:
        for path in repo.rglob(pattern):
            parts = set(path.parts)
            if ".git" in parts or "node_modules" in parts:
                continue
            if len(digests) >= limit:
                return digests
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
            except OSError:
                continue
            digests[str(path.relative_to(repo))] = digest
    return dict(sorted(digests.items()))


def compare(baseline, current):
    """The differences, as a list of human-readable strings."""
    findings = []
    if baseline["head"] != current["head"]:
        findings.append(
            f"HEAD moved: {baseline['head'][:12]} -> {current['head'][:12]}")
    if baseline["branch"] != current["branch"]:
        findings.append(
            f"branch changed: {baseline['branch']} -> {current['branch']}")
    new_worktrees = set(current["worktrees"]) - set(baseline["worktrees"])
    if new_worktrees:
        findings.append(f"new worktree(s): {sorted(new_worktrees)}")
    removed_worktrees = set(baseline["worktrees"]) - set(current["worktrees"])
    if removed_worktrees:
        findings.append(f"worktree(s) removed: {sorted(removed_worktrees)}")
    # The status set is compared, not required to be empty. The neighbouring
    # repository has other work in flight by design, so "not clean" is the
    # normal condition and reporting it as interference would make this tool
    # cry wolf on every run -- and a check that always fails is one nobody
    # runs. What matters is whether *this* project changed anything.
    added = set(current["status"]) - set(baseline["status"])
    removed = set(baseline["status"]) - set(current["status"])
    for line in sorted(added):
        findings.append(f"new working-tree change: {line}")
    for line in sorted(removed):
        findings.append(f"working-tree change gone: {line}")
    before, after = baseline["state_files"], current["state_files"]
    for name in sorted(set(after) - set(before)):
        findings.append(f"state file appeared: {name}")
    for name in sorted(set(before) - set(after)):
        findings.append(f"state file disappeared: {name}")
    for name in sorted(set(before) & set(after)):
        if before[name] != after[name]:
            findings.append(
                f"state file changed: {name} "
                f"({before[name]} -> {after[name]})")
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True,
                        help="the neighbouring repository to watch")
    parser.add_argument("--record", required=True,
                        help="path to the baseline snapshot file")
    parser.add_argument("--check", action="store_true",
                        help="compare against the baseline and fail on drift")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.repo):
        print(f"[error] {args.repo} is not a directory", file=sys.stderr)
        return 2
    current = snapshot(args.repo)

    if not args.check:
        Path(args.record).write_text(json.dumps(current, indent=2,
                                                sort_keys=True),
                                     encoding="utf-8")
        print(f"baseline recorded: {args.record}")
        print(f"  head      {current['head'][:12]}")
        print(f"  worktrees {len(current['worktrees'])}")
        print(f"  status    {len(current['status'])} entries")
        return 0

    baseline_path = Path(args.record)
    if not baseline_path.exists():
        print(f"[error] no baseline at {args.record}; run without --check first",
              file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    findings = compare(baseline, current)
    if not findings:
        print(f"[clear] no interference with {args.repo}")
        return 0
    print(f"[INTERFERENCE] {len(findings)} difference(s) in {args.repo}:")
    for finding in findings:
        print(f"  - {finding}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
