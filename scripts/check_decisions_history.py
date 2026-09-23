#!/usr/bin/env python3
"""check_decisions_history.py — decision ledger entries are immutable.

``docs/decisions/`` is an append-only record of product and design decisions a
person on the team made. Each entry is one file; the directory's README is the
front page and index. The rules are in ``docs/decisions/README.md``; this gate
enforces the one that can be checked without reading:

    every ``docs/decisions/*.md`` present at the base ref, except ``README.md``,
    must exist at head byte-identical

A deleted entry, a renamed entry and an edited entry all fail. A new regular-file
entry is allowed, because appending is the only way the ledger grows. Entries
must be regular files at both base and head: a link resolves to whatever its
target says, so its resolved bytes are not the entry's bytes. Base entries and
file modes come from Git's tree, not the checkout. The ledger directory itself
must be a tree at base and a real directory at head, not a link to another
ledger. Head entries are checked without following links before their bytes are
read. The README is exempt because adding an entry means adding its index row,
and docs-lint refuses an unindexed doc.

## Why this is a deterministic gate and not a review rule

An entry exists so that a later change does not quietly reverse a decision. The
easiest way to reverse one is to make the record say something else, or to make
it say nothing: an "edit" that softens a sentence, a rename that drops the file
out of a grep, a deletion buried in a hunk beside a real change. All three read
as plausible in review, because the reviewer looks at what was added. So the
judgment half of the rule (does this change reverse a recorded decision without
a superseding entry?) stays in ``AUTOSDE.yaml``, where a reviewer applies it, and
the mechanical half lives here, where no reviewer has to notice anything.

A change of mind is a NEW entry naming the one it supersedes, so the correct
shape of a ledger diff is additions only, and this gate has **no exemptions**.

## Usage

    # enforce against a base ref (exit 1 on any violation)
    DECISIONS_BASE_REF="$(git merge-base HEAD origin/main)" \\
        python3 scripts/check_decisions_history.py

    # self-test: one probe per defect class, assert each verdict
    python3 scripts/check_decisions_history.py --test

With no ``DECISIONS_BASE_REF`` the check still rejects non-regular head entries,
then exits 0 with a note when there is nothing to compare against.

Pass the MERGE BASE, not the base branch's moving tip. CI passes
``pull_request.base.sha`` and tests the merge ref, so the two agree there; locally
they do not. A branch that is merely BEHIND a base which has since gained an
entry reports that entry as missing — true and useless. The merge base is the
commit the branch actually departed from, so it answers "did THIS branch remove
or rewrite anything".
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

DECISIONS_DIR = "docs/decisions"
INDEX = "README.md"


def _git(*args: str) -> str | None:
    """Run ``git`` and return stdout, or ``None`` when the command fails."""
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def entries_at(ref: str, directory: str = DECISIONS_DIR) -> dict[str, bytes]:
    """Read flat regular-file entries from Git's tree, rejecting other modes.

    An absent ledger is empty. A failed enumeration or blob read is a violation,
    not evidence that the base has nothing to protect. Check the directory's
    exact record because recursive listings of a link contain no child entries.
    """
    exact = _git("ls-tree", "-z", ref, "--", directory)
    if exact is None:
        raise ValueError(f"Cannot enumerate {directory} at {ref} with git ls-tree.")
    if not exact:
        return {}
    for record in exact.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, kind, _oid = metadata.split()
        if path != directory or mode != "040000" or kind != "tree":
            reason = "a symlink" if mode == "120000" else f"Git mode {mode} ({kind})"
            raise ValueError(
                f"{directory} at {ref} is {reason}. The ledger directory itself "
                "must be a tree; a non-tree record is not an absent ledger."
            )
    listing = _git("ls-tree", "-r", "-z", ref, "--", directory)
    if listing is None:
        raise ValueError(f"Cannot enumerate {directory} at {ref} with git ls-tree.")
    found: dict[str, bytes] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, kind, oid = metadata.split()
        prefix = f"{directory}/"
        if not path.startswith(prefix):
            continue
        name = path[len(prefix) :]
        if not name.endswith(".md") or name == INDEX or "/" in name:
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            reason = "a symlink" if mode == "120000" else f"Git mode {mode} ({kind})"
            raise ValueError(
                f"{path} at {ref} is {reason}. An entry must be a regular file "
                "the gate can compare byte-for-byte."
            )
        blob = subprocess.run(
            ["git", "cat-file", "blob", oid],
            capture_output=True,
            check=False,
        )
        if blob.returncode != 0:
            raise ValueError(f"Cannot read {path} at {ref} byte-for-byte.")
        found[name] = blob.stdout
    return found


def entries_in_tree(root: Path) -> dict[str, bytes]:
    """Read flat head entries, rejecting links and non-regular files before reading.

    A link's resolved bytes belong to its target, not to the ledger entry.
    The ledger directory and every directory between the repository root and
    it are checked before enumerating children: a link anywhere on that path
    makes the gate compare whatever the link points at.
    """
    directory = root / DECISIONS_DIR
    walked = root
    for part in Path(DECISIONS_DIR).parts:
        walked = walked / part
        try:
            walked_mode = os.lstat(walked).st_mode
        except FileNotFoundError:
            return {}
        if not stat.S_ISDIR(walked_mode):
            reason = "a symlink" if stat.S_ISLNK(walked_mode) else "not a directory"
            raise ValueError(
                f"{walked} is {reason}. The ledger directory and every directory "
                "above it must be a real directory; a link makes the gate compare "
                "whatever the link points at."
            )
    found: dict[str, bytes] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".md" or path.name == INDEX:
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            reason = "a symlink" if stat.S_ISLNK(mode) else "not a plain regular file"
            raise ValueError(
                f"{path} is {reason}. An entry must be a regular file "
                "the gate can compare byte-for-byte."
            )
        found[path.name] = path.read_bytes()
    return found


def compare(base: dict[str, bytes], head: dict[str, bytes]) -> list[str]:
    """Every base entry that is missing or changed at head, as problem lines.

    An entry that exists at head under a different name is reported as
    renamed, so the message points at the fix (restore the file) rather than at
    two unrelated symptoms.
    """
    problems: list[str] = []
    head_by_content: dict[bytes, list[str]] = {}
    for name, content in head.items():
        head_by_content.setdefault(content, []).append(name)
    for name in sorted(base):
        content = base[name]
        if name not in head:
            renamed = [n for n in head_by_content.get(content, []) if n not in base]
            if renamed:
                problems.append(
                    f"{DECISIONS_DIR}/{name} was RENAMED to {DECISIONS_DIR}/{renamed[0]}. "
                    f"An entry keeps its name for as long as the ledger exists; a rename "
                    f"drops it out of every grep and link that found it."
                )
            else:
                problems.append(
                    f"{DECISIONS_DIR}/{name} is GONE. An entry is never deleted; a "
                    f"change of mind is a new entry with a 'Supersedes:' line."
                )
        elif head[name] != content:
            problems.append(
                f"{DECISIONS_DIR}/{name} was EDITED. An entry is never edited after it "
                f"is written; a change of mind is a new entry with a 'Supersedes:' line."
            )
    return problems


def _self_test() -> int:
    """Probe immutability, regular-file enforcement and allowed ledger shapes."""
    entry = b"# Buttons say New\n\nDecided by: A Person\n"
    other = b"# Panels are blue\n\nDecided by: A Person\n"
    base = {"2026-07-20-buttons-say-new.md": entry, "2026-08-01-panels-are-blue.md": other}
    cases: list[tuple[str, dict[str, bytes], bool]] = [
        (
            "a deleted entry is caught",
            {"2026-08-01-panels-are-blue.md": other},
            True,
        ),
        (
            "an edited entry is caught",
            {
                "2026-07-20-buttons-say-new.md": entry + b"\nEDITED\n",
                "2026-08-01-panels-are-blue.md": other,
            },
            True,
        ),
        (
            "a renamed entry is caught",
            {"2026-07-20-buttons-say-new-chat.md": entry, "2026-08-01-panels-are-blue.md": other},
            True,
        ),
        (
            "a new entry is allowed",
            {**base, "2026-09-23-a-third-decision.md": b"# Third\n"},
            False,
        ),
        (
            "a superseding entry beside the untouched original is allowed",
            {
                **base,
                "2026-09-23-buttons-say-create.md": b"# Create\n\nSupersedes: 2026-07-20-buttons-say-new.md\n",
            },
            False,
        ),
        ("an unchanged ledger is clean", dict(base), False),
    ]
    failures: list[str] = []
    for name, head, expect in cases:
        got = bool(compare(base, head))
        if got != expect:
            failures.append(f"{name}: expected violation={expect}, got {got}")

    # The rename probe must name the rename, not report an unrelated deletion,
    # or the message sends the author looking for a file that is still there.
    renamed = compare(
        base, {"2026-07-20-buttons-say-new-chat.md": entry, "2026-08-01-panels-are-blue.md": other}
    )
    if not any("RENAMED" in line for line in renamed):
        failures.append("a rename is reported as something other than a rename")

    # The README is the index, not an entry: editing it is how a row is added,
    # so the tree walk must never surface it. A nested directory is not an entry
    # either (one flat file per decision), so it must not surface one from there.
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp) / DECISIONS_DIR
        (ledger / "nested").mkdir(parents=True)
        (ledger / INDEX).write_bytes(b"# Decisions\n")
        (ledger / "2026-07-20-buttons-say-new.md").write_bytes(entry)
        (ledger / "nested" / "2026-01-01-not-an-entry.md").write_bytes(b"# Nested\n")
        (ledger / "notes.txt").write_bytes(b"not markdown")
        seen = entries_in_tree(Path(tmp))
        if set(seen) != {"2026-07-20-buttons-say-new.md"}:
            failures.append(f"entries_in_tree surfaced the wrong files: {sorted(seen)}")
        # ...and a tree with no ledger at all is an empty set, not an error.
        if entries_in_tree(Path(tmp) / "elsewhere") != {}:
            failures.append("a tree without docs/decisions/ is not an empty ledger")

    probes = len(cases) + 4
    skipped = 0
    for replace in (False, True):
        label = "replacement symlink" if replace else "new identical-content symlink"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.mkdir(parents=True)
            target = root / "original.txt"
            path = ledger / "2026-07-20-buttons-say-new.md"
            if replace:
                path.write_bytes(entry)
                path.rename(target)
            else:
                target.write_bytes(entry)
            try:
                os.symlink(target, path)
            except (OSError, NotImplementedError) as exc:
                print(f"self-test SKIP: {label}: symlink creation unavailable ({exc})")
                skipped += 1
                continue
            probes += 1
            try:
                entries_in_tree(root)
            except ValueError as exc:
                if str(path) not in str(exc) or "symlink" not in str(exc):
                    failures.append(f"{label}: violation does not name the link and reason")
            else:
                failures.append(f"{label}: accepted a link to identical bytes")

    # "ancestor" links the parent directory instead of the ledger itself: a link
    # one level up resolves to the same bytes and must be refused just the same.
    for shape in ("symlink", "blob", "ancestor"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.mkdir(parents=True)
            for name, content in base.items():
                (ledger / name).write_bytes(content)
            linked = ledger.parent if shape == "ancestor" else ledger
            target = root / "original-ledger"
            linked.rename(target)
            if shape in ("symlink", "ancestor"):
                try:
                    os.symlink(target, linked, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    print(f"self-test SKIP: ledger {shape}: creation unavailable ({exc})")
                    skipped += 1
                    continue
            else:
                ledger.write_bytes(entry)
            probes += 1
            try:
                entries_in_tree(root)
            except ValueError as exc:
                if str(linked) not in str(exc) or "real directory" not in str(exc):
                    failures.append(f"head ledger {shape}: violation lacks path and reason")
            else:
                failures.append(f"head ledger {shape}: accepted a non-directory ledger")

    # Git trees expose a directory's own mode independently of its children.
    # A throwaway index builds base trees without requiring commits or identity.
    for shape in ("absent", "tree", "blob", "symlink"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / DECISIONS_DIR
            ledger.parent.mkdir(parents=True)
            if shape == "tree":
                ledger.mkdir()
                for name, content in base.items():
                    (ledger / name).write_bytes(content)
            elif shape == "blob":
                ledger.write_bytes(entry)
            elif shape == "symlink":
                target = root / "original-ledger"
                target.mkdir()
                for name, content in base.items():
                    (target / name).write_bytes(content)
                try:
                    os.symlink(target, ledger, target_is_directory=True)
                except (OSError, NotImplementedError) as exc:
                    print(f"self-test SKIP: base ledger symlink: creation unavailable ({exc})")
                    skipped += 1
                    continue
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                if _git("init", "--quiet") is None:
                    failures.append(f"base ledger {shape}: cannot initialize probe repository")
                    continue
                if shape != "absent" and _git("add", "--", DECISIONS_DIR) is None:
                    failures.append(f"base ledger {shape}: cannot stage probe ledger")
                    continue
                tree = _git("write-tree")
                if tree is None:
                    failures.append(f"base ledger {shape}: cannot build probe tree")
                    continue
                probes += 1
                try:
                    seen = entries_at(tree.strip())
                except ValueError as exc:
                    if shape not in {"blob", "symlink"}:
                        failures.append(f"base ledger {shape}: unexpected violation: {exc}")
                    elif DECISIONS_DIR not in str(exc) or "must be a tree" not in str(exc):
                        failures.append(f"base ledger {shape}: violation lacks path and reason")
                else:
                    if shape in {"blob", "symlink"}:
                        failures.append(f"base ledger {shape}: accepted a non-tree ledger")
                    elif seen != (base if shape == "tree" else {}):
                        failures.append(f"base ledger {shape}: read incorrect entries")
            finally:
                os.chdir(original_cwd)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = root / DECISIONS_DIR / "2026-07-20-buttons-say-new.md"
        path.mkdir(parents=True)
        probes += 1
        try:
            entries_in_tree(root)
        except ValueError as exc:
            if str(path) not in str(exc) or "regular file" not in str(exc):
                failures.append("directory entry: violation does not name the path and reason")
        else:
            failures.append("a directory named as an entry is accepted")

    # Nothing at base means nothing to protect: the very first entry lands
    # through this gate, so an empty base must not refuse it.
    if compare({}, {"2026-09-23-first.md": b"# First\n"}):
        failures.append("a ledger with nothing at base refuses its first entry")

    if failures:
        for line in failures:
            print(f"self-test FAILED: {line}", file=sys.stderr)
        return 1
    print(f"decision-ledger-history self-test: {probes} probes, all correct; {skipped} skipped")
    return 0


def main(argv: list[str]) -> int:
    if "--test" in argv:
        return _self_test()

    root = Path(__file__).resolve().parents[1]
    try:
        head = entries_in_tree(root)
    except (ValueError, OSError) as exc:
        print(f"::error::decision-ledger-history: {exc}", file=sys.stderr)
        return 1

    base_ref = os.environ.get("DECISIONS_BASE_REF", "").strip()
    if not base_ref:
        print(
            "decision-ledger-history: no DECISIONS_BASE_REF, so nothing to compare "
            "the ledger against (set it to enforce, e.g. DECISIONS_BASE_REF=origin/main)"
        )
        return 0

    try:
        base = entries_at(base_ref)
    except (ValueError, OSError) as exc:
        print(f"::error::decision-ledger-history: {exc}", file=sys.stderr)
        return 1

    problems = compare(base, head)
    if problems:
        for problem in problems:
            print(f"::error::decision-ledger-history: {problem}", file=sys.stderr)
        print(
            "\nA decision record is append-only. To change what the team decided, "
            "add a new entry that names the one it supersedes and quotes the "
            "maintainer who decided; leave the original file exactly as it is. "
            "See docs/decisions/README.md.",
            file=sys.stderr,
        )
        return 1

    print(
        f"decision-ledger-history: {len(base)} entr{'y' if len(base) == 1 else 'ies'} "
        f"at {base_ref} intact at head OK"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
