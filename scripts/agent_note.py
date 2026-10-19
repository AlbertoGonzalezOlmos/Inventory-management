#!/usr/bin/env python3
"""agent_note — collision-safe append to a shared agent coordination file.

Several independent Pi sessions coordinate through one Markdown file
(``SCANNER-AGENT-SYNC.md``). Concurrent writers that use read-modify-write
editors (the ``edit``/``write`` tools) lose data: one reads the file, another
appends, the first writes back its stale copy and clobbers the append. That is
not hypothetical — it happened (verify-hw vs qwen, 2026-10-10).

This helper makes writes append-only and atomic:

    uv run python scripts/agent_note.py --author qwen \\
        --file SCANNER-AGENT-SYNC.md --mirror /tmp/scanner-agent-sync.md <<'EOF'
    what I found, with the command and its output
    EOF

An exclusive ``flock`` on ``<file>.lock`` serialises concurrent appends, and
each block is written with a single ``O_APPEND`` write, so a slow writer can
never overwrite a faster one. ``--mirror`` syncs the merged file to a second
path for peers whose only inbound channel is the copy (the ghostty sessions)
— as a GUARDED sync: the append happens first (the canonical file never
loses the author's finding), then, if the mirror carries lines the file
lacks (peer updates that only ever reached the mirror — this happened three
times on 2026-10-10), the sync is REFUSED (exit 3): the offending lines are
printed, nothing is copied, and the recovery is printed with them. Never
clobber, never double-append.

Exit codes: 0 = appended (+ mirrored); 2 = empty input, nothing appended;
3 = appended, but the mirror sync was refused (see the printed recovery).

Rule: never use ``edit``/``write`` on a shared coordination file — append only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import os
import shutil
import sys


def build_block(author: str, text: str, when: dt.datetime | None = None) -> str:
    """One append block, prefixed with an ISO timestamp and the author."""
    when = when or dt.datetime.now().astimezone()
    stamp = when.isoformat(timespec="seconds")
    return f"\n### {stamp} — {author}\n\n{text.rstrip(chr(10))}\n"


def append_block(path: str, block: str) -> None:
    """Append *block* under an exclusive lock; atomic against other appenders."""
    with open(path + ".lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            with open(path, "a", encoding="utf-8") as out:
                out.write(block)
                out.flush()
                os.fsync(out.fileno())
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def mirror_only_lines(path: str, mirror: str) -> list[str]:
    """The lines the mirror carries that *path* does not — exactly what a
    repo→mirror ``copyfile`` would destroy.

    A stripped, non-empty LINE-SET diff (order-insensitive: the file is
    append-only and ``build_block`` timestamps make blocks unique),
    returned in mirror order so the refusal message reads like the file.
    A missing mirror has nothing to lose → ``[]``; so does one that only
    holds whitespace the file lacks.
    """
    try:
        with open(mirror, encoding="utf-8") as fh:
            mirror_lines = fh.read().splitlines()
    except OSError:
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            file_lines = {ln.strip() for ln in fh.read().splitlines()
                          if ln.strip()}
    except OSError:
        file_lines = set()
    return [ln.strip() for ln in mirror_lines
            if ln.strip() and ln.strip() not in file_lines]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Append a block to a shared agent coordination file safely")
    ap.add_argument("--file", required=True, help="coordination file to append to")
    ap.add_argument("--author", required=True,
                    help="writer identity for the block header")
    ap.add_argument("--mirror", default=None,
                    help="copy the merged file here after appending")
    args = ap.parse_args(argv)

    text = sys.stdin.read()
    if not text.strip():
        print("agent_note: empty input; nothing appended", file=sys.stderr)
        return 2
    # APPEND FIRST (D5): the canonical file must never lose the author's
    # finding, whatever the mirror state — a refused sync must not cost the
    # append. The mirror check follows, and a refusal must NOT say "re-run":
    # re-running would append the same block twice.
    append_block(args.file, build_block(args.author, text))
    if args.mirror:
        conflicts = mirror_only_lines(args.file, args.mirror)
        if conflicts:
            print(f"agent_note: REFUSING the mirror sync — {args.mirror} "
                  f"holds {len(conflicts)} line(s) {args.file} lacks; a sync "
                  "now would destroy them:", file=sys.stderr)
            for ln in conflicts:
                print(f"  | {ln}", file=sys.stderr)
            print(f"Your block WAS appended to {args.file} (nothing was lost "
                  "there). Recover by merging the lines above into "
                  f"{args.file} by hand — or accepting their loss — then "
                  f"`rm {args.mirror}`: the next append re-seeds the mirror.",
                  file=sys.stderr)
            return 3
        shutil.copyfile(args.file, args.mirror)
    print(f"appended {len(text)} chars as {args.author!r} to {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
