#!/usr/bin/env bash
# Materialize the review evidence a FORK pull request COMMITS, straight out of
# the object store -- for the contributors who cannot attach it to the PR
# description at all.
#
# `gh pr create|edit --attach` uploads through an endpoint that answers READ
# and TRIAGE permission with a 404 (cli/cli#14302), so a contributor working
# from a fork has no CLI path to a description attachment; dragging the file
# into the web editor is their only one. The committed convention therefore
# stays open to them: media added under `temp-screenshots/` or
# `.github/screenshots/` (both gitignored, so it takes `git add -f`) is review
# evidence too, and this script is what puts it in front of the fork lanes'
# reviewer, which never checks the fork head out.
#
# Sourced (not executed) AFTER pr-attachment-evidence.sh by the fork UX lane,
# so `$n` (images kept) and `$clips` (recordings listed) continue under the
# same MAX_SHOTS/MAX_CLIPS caps: a description attachment is the normal home
# for evidence and must never be dropped by the cap in favour of a committed
# file. It is a separate file from that one because the bytes come from a
# different place -- a git object rather than an HTTP download -- and because
# four lanes source the attachment script, whose behaviour this must not
# change.
#
# Inputs, all environment variables:
#   BASE_SHA, HEAD_SHA   revision range; every blob is read AT HEAD_SHA
#   FETCH_DIR            scratch dir for the bytes before they are typed
#   DEST_DIR             where kept images land, as "$NAME_STEM-NN.<ext>"
#   NAME_STEM            copy-name stem, shared with the attachment pass
#   SHOTS, SHOT_MAP, CLIPS  list files this APPENDS to: kept image paths,
#                        "<name>\t<origin>" origins, recording origins
#   MAX_SHOTS, MAX_CLIPS caps, shared with the attachment pass
#
# Every byte here is UNTRUSTED fork content, so nothing about the committed
# file reaches disk except its bytes. The path never becomes a destination
# name (each copy is "$NAME_STEM-NN.<ext>", index-named, as the blind-read
# wall requires); the type comes from file(1) reading the bytes, never from
# the extension; and the blob is read with `git cat-file` instead of from a
# checkout, so the fork's tree is never materialized -- a tracked symlink, a
# mode bit, or a path like `.git/hooks/pre-commit` cannot exist on disk to be
# followed. A tree entry that is not a plain blob (a symlink is mode 120000, a
# submodule 160000) is skipped BY ITS MODE, before its bytes are read.
set -euo pipefail
mkdir -p "$FETCH_DIR" "$DEST_DIR"
# Continue the attachment pass's counters when it ran first, and stand alone
# when it did not, so the caps below bound the two sources TOGETHER.
n="${n:-0}"
clips="${clips:-0}"
committed_found=0
committed_kept=0
committed_skipped=0
# The attachment pass leaves its per-download attempt count in `fetched`.
# Start there so the expensive-read budget covers both evidence sources; a
# standalone invocation starts at zero.
committed_read="${fetched:-0}"
# `-z` because a path git would otherwise quote (a space, a non-ASCII byte) is
# a path this loop must still read exactly. --diff-filter=AM: a deleted
# screenshot has no bytes to show. The pathspec pins the two committed-
# screenshot conventions, and the extension allowlist keeps a README or a
# stray text file in those directories from costing a blob read; the bytes
# still decide the type afterwards.
#
# The listing goes to a file before the loop reads it, so that a git failure
# is a failure: the exit status of a process substitution is invisible to
# `set -e`, and an empty listing would read as "this PR commits no media",
# which the design lane reports as evidence the author never supplied. The
# attachment script fails its step the same way when the description cannot
# be read -- a red step the author can re-run, never a silent "nothing here".
paths="$(mktemp "$FETCH_DIR/committed-paths.XXXXXX")"
if ! git diff -z --name-only --diff-filter=AM "$BASE_SHA...$HEAD_SHA" -- \
    temp-screenshots .github/screenshots > "$paths"; then
  echo "::error::Could not enumerate the media committed between $BASE_SHA and $HEAD_SHA (git diff failed, see above), so the committed evidence cannot be collected; re-run the workflow."
  # Record the lane's own failure before leaving, because `exit 1` from a
  # SOURCED script skips the rest of the caller's step, including the line
  # that writes this output. The fork lanes' Finalize step resolves an errored
  # advisory run as NEUTRAL, which PR readiness scores as a pass, so without
  # this the step goes red while the check-run says "review incomplete
  # (advisory)" and nothing blocks. `unfetched` is the output both fork lanes
  # already turn into a FAILED check-run naming the re-run as the remedy; the
  # design lane does not read it, so writing it there is inert.
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "unfetched=true" >> "$GITHUB_OUTPUT"
  fi
  rm -f -- "$paths"
  exit 1
fi
while IFS= read -r -d '' path; do
  [ -n "$path" ] || continue
  # Lower-cased through `tr` in the C locale: it folds the ASCII letters and
  # passes every other byte through, so a non-ASCII name still matches its
  # extension, and it runs under the bash 3.2 of macOS, which has no `${var,,}`
  # and aborts the whole step on it.
  lower="$(printf '%s' "$path" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
  case "$lower" in
    *.png|*.jpg|*.jpeg|*.webp|*.gif|*.webm|*.mp4|*.mov) ;;
    *) continue ;;
  esac
  committed_found=$((committed_found + 1))
  # The path is fork-controlled text, and below this point it is written
  # into $SHOT_MAP and $CLIPS -- files the reviewer's prompt presents as
  # written by the workflow -- and into the `::warning::` and TRUNCATED lines
  # of the log. Git allows a newline, a tab or a carriage return in a tracked
  # name, and one such byte forges an extra record in the data file or a
  # workflow command in the log. So a name carrying any control character is
  # refused HERE, above the first line that interpolates `$path` anywhere:
  # every sink is below this check, and none of them can be reached with
  # such a name. The refusal prints the name `%q`-quoted, never raw.
  case "$path" in
    *[[:cntrl:]]*)
      echo "::warning::SKIPPED (path contains a control character): $(printf '%q' "$path")"
      committed_skipped=$((committed_skipped + 1))
      continue ;;
  esac
  # Mode and size from the tree itself, so a symlink or an oversized blob is
  # refused before anything is read. `ls-tree` takes a PATHSPEC where
  # `cat-file` below takes an exact path, so the pathspec is pinned with
  # `:(literal)`: a `*`, `[`, `?` or `!` in a committed name then names that
  # one entry, and the mode and size gates judge the same blob whose bytes
  # are read. `git ls-tree -l` prints "<mode> <type> <oid> <size>\t<path>".
  meta="$(git ls-tree -l "$HEAD_SHA" -- ":(literal)$path" 2>/dev/null || true)"
  mode="$(awk '{print $1; exit}' <<< "$meta")"
  size="$(awk '{print $4; exit}' <<< "$meta")"
  case "$mode" in
    100644|100755) ;;
    *)
      echo "::warning::SKIPPED (not a regular file at $HEAD_SHA, mode ${mode:-unknown}): $path"
      committed_skipped=$((committed_skipped + 1))
      continue ;;
  esac
  # One ceiling for every committed blob, image or recording: 10 MB, GitHub's
  # per-image attachment limit. A committed file is permanent history that
  # every clone carries, and a recording is the worst case for that cost, so
  # the 100 MB the attachment path allows a download -- transient bytes on a
  # runner -- does not carry over to a video here. A recording that needs
  # more has a home that costs the repository nothing: dragged into the
  # description in the web editor, which the attachment script reads.
  if [ "${size:-0}" -gt 10485760 ]; then
    echo "::warning::SKIPPED (${size} bytes, over the 10 MB ceiling): $path"
    committed_skipped=$((committed_skipped + 1))
    continue
  fi
  # Tree metadata is cheap and the diff bounds it. Blob materialization and
  # byte typing consume this budget whether the candidate is accepted or
  # skipped, matching the attachment script's per-download attempt counter.
  if [ "$committed_read" -ge "$((MAX_SHOTS + MAX_CLIPS))" ]; then
    echo "TRUNCATED: more than $((MAX_SHOTS + MAX_CLIPS)) pieces of evidence; not read: $path"
    continue
  fi
  committed_read=$((committed_read + 1))
  tmp="$(mktemp "$FETCH_DIR/committed.XXXXXX")"
  if ! git cat-file blob "$HEAD_SHA:$path" > "$tmp" 2>/dev/null; then
    echo "::warning::SKIPPED (blob not readable at $HEAD_SHA): $path"
    committed_skipped=$((committed_skipped + 1))
    rm -f -- "$tmp"
    continue
  fi
  # Type by bytes, never by the path: a fork controls the name it commits. SVG
  # is left out on purpose, as in the attachment script -- the Read tool opens
  # it as markup, not as pixels.
  mime="$(file --mime-type -b -- "$tmp")"
  case "$mime" in
    image/png) ext=png ;;
    image/jpeg) ext=jpg ;;
    image/webp) ext=webp ;;
    image/gif) ext=gif ;;
    video/mp4) ext=mp4 ;;
    video/quicktime) ext=mov ;;
    video/webm) ext=webm ;;
    *)
      echo "::warning::SKIPPED (mime $mime): $path"
      committed_skipped=$((committed_skipped + 1))
      rm -f -- "$tmp"
      continue ;;
  esac
  case "$ext" in
    mp4|mov|webm|gif)
      if [ "$clips" -lt "$MAX_CLIPS" ]; then
        clips=$((clips + 1))
        printf '%s\n' "$path" >> "$CLIPS"
      else
        echo "TRUNCATED: more than $MAX_CLIPS recordings; one was not listed" >> "$CLIPS"
      fi ;;
  esac
  case "$ext" in
    png|jpg|webp|gif)
      # A GIF is both: the model can open its first frame, and its existence
      # is what the continuity lens asks about.
      if [ "$n" -lt "$MAX_SHOTS" ]; then
        n=$((n + 1))
        committed_kept=$((committed_kept + 1))
        name="$(printf '%s-%02d.%s' "$NAME_STEM" "$n" "$ext")"
        mv -- "$tmp" "$DEST_DIR/$name"
        printf '%s\n' "$DEST_DIR/$name" >> "$SHOTS"
        printf '%s\t%s\n' "$name" "$path" >> "$SHOT_MAP"
        continue
      else
        echo "TRUNCATED: more than $MAX_SHOTS images; one was not listed" >> "$SHOTS"
        printf 'TRUNCATED\t%s\n' "$path" >> "$SHOT_MAP"
      fi ;;
  esac
  rm -f -- "$tmp"
done < "$paths"
rm -f -- "$paths"
echo "Committed evidence: $committed_found media path(s) added or changed under temp-screenshots/ or .github/screenshots/, $committed_kept image(s) kept, $committed_skipped skipped."
