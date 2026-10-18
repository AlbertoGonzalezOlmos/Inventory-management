"""Tests for scripts/agent_note.py — atomic append for shared coordination files.

The failure this prevents is real: verify-hw's read-modify-write edit of
SCANNER-AGENT-SYNC.md clobbered a concurrent append from qwen (2026-10-10).
"""

import datetime as dt
import io
import threading

from scripts.agent_note import (
    append_block,
    build_block,
    main,
    mirror_only_lines,
)


def test_build_block_has_timestamp_author_and_body():
    when = dt.datetime(2026, 10, 10, 22, 55, tzinfo=dt.timezone.utc)
    block = build_block("qwen", "line one\nline two\n\n", when=when)
    assert block.startswith("\n### 2026-10-10T22:55:00+00:00 — qwen\n")
    assert block.endswith("line one\nline two\n")
    assert not block.endswith("\n\n")  # trailing newlines normalised


def test_append_block_preserves_existing_content(tmp_path):
    f = tmp_path / "sync.md"
    f.write_text("# header\n")
    append_block(str(f), build_block("a", "first"))
    append_block(str(f), build_block("b", "second"))
    text = f.read_text()
    assert text.startswith("# header\n")
    assert text.index("first") < text.index("second")


def test_concurrent_appends_do_not_lose_writers(tmp_path):
    f = tmp_path / "sync.md"
    f.write_text("")
    n = 24

    def writer(i):
        append_block(str(f), build_block(f"w{i}", f"msg-{i}"))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = f.read_text()
    for i in range(n):
        assert f"msg-{i}" in text, f"writer {i} was lost"


# --- the --mirror guard (append-then-refuse, never clobber) -------------------

def _run_main(tmp_path, monkeypatch, text, *, mirror, file_content=None):
    f = tmp_path / "sync.md"
    if file_content is not None:
        f.write_text(file_content)
    m = tmp_path / mirror
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    code = main(["--file", str(f), "--author", "probe", "--mirror", str(m)])
    return code, f, m


def test_mirror_refusal_appends_then_preserves(tmp_path, monkeypatch, capsys):
    """The one-way copyfile destroyed mirror-only peer updates three times
    (2026-10-10). Now: the append lands FIRST (the canonical file never
    loses the finding), the mirror is left BYTE-IDENTICAL, the offending
    lines are printed, and the recovery is merge-by-hand + rm — not
    're-run', which would double-append."""
    m = tmp_path / "mirror.md"
    m.write_text("### 2026-10-10T22:55:00+02:00 — kimi\n\nmirror-only finding\n")
    code, f, m = _run_main(tmp_path, monkeypatch, "my new finding",
                           mirror="mirror.md")
    assert code == 3
    # the append DID land (append-then-refuse, never refuse-the-append)
    assert "my new finding" in f.read_text()
    # the mirror is untouched — the peer update survives
    assert "mirror-only finding" in m.read_text()
    assert "### 2026-10-10T22:55:00+02:00 — kimi" in m.read_text()
    err = capsys.readouterr().err
    assert "mirror-only finding" in err          # the offending lines printed
    assert "rm" in err and "merging" in err.lower()  # the recovery instruction


def test_mirror_subset_is_synced(tmp_path, monkeypatch):
    """The normal case: the mirror is behind (a strict subset) — nothing to
    lose, sync proceeds."""
    code, f, m = _run_main(tmp_path, monkeypatch, "first finding",
                           mirror="mirror.md")
    assert code == 0
    assert m.read_text() == f.read_text()
    code, f, m = _run_main(tmp_path, monkeypatch, "second finding",
                           mirror="mirror.md", file_content=f.read_text())
    assert code == 0
    assert m.read_text() == f.read_text()
    assert "second finding" in m.read_text()


def test_missing_mirror_is_created(tmp_path, monkeypatch):
    code, f, m = _run_main(tmp_path, monkeypatch, "first finding",
                           mirror="fresh-mirror.md")
    assert code == 0
    assert m.exists() and m.read_text() == f.read_text()


def test_mirror_only_lines_ignores_whitespace_and_order(tmp_path):
    """A mirror differing only in trailing whitespace or line order is NOT
    a conflict — the guard must not cry wolf and push agents back to
    edit/write (the exact data-loss class it exists to prevent)."""
    f = tmp_path / "sync.md"
    m = tmp_path / "mirror.md"
    f.write_text("alpha\nbeta\n")
    m.write_text("beta   \n\nalpha\n\n")
    assert mirror_only_lines(str(f), str(m)) == []
    m.write_text("beta\ngamma\n")
    assert mirror_only_lines(str(f), str(m)) == ["gamma"]
    assert mirror_only_lines(str(f), str(tmp_path / "absent.md")) == []