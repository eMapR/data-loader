#!/bin/bash
# Move an imagery library to another filesystem (docs/library-transfer.md).
#
#   transfer_library.sh prepare SRC KIT     record every file of SRC (size, mtime), every symlink,
#                                           the SHA-256 of every non-imagery file, and SRC's tile table
#   (copy: plain rsync into a staged root, see docs/library-transfer.md)
#   transfer_library.sh check KIT DEST      after the copy: same files/sizes/mtimes/symlinks,
#                                           non-imagery SHA-256, full imagery verify against the
#                                           recorded SHA-256 (imagery-library verify), catalog,
#                                           per-tile counts equal to SRC's
#   transfer_library.sh promote KIT FINAL   move the checked staged root's entries into the permanent
#                                           root FINAL (same filesystem: renames), LIBRARY.yaml last
#
# Nothing here writes to SRC. staging/ is never transferred (it is per filesystem).
set -euo pipefail
IL=${IMAGERY_LIBRARY:-/vol/v1/data-loader-lib/.venv/bin/imagery-library}
export LC_ALL=C

die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo "== $*"; }

# path<TAB>size<TAB>mtime(s) of every file, and path<TAB>target of every symlink, under $1
list_files() {
    (cd "$1" && find . -path ./staging -prune -o -name .rsync-partial -prune -o -type f -printf '%P\t%s\t%T@\n' \
        | awk -F'\t' '{printf "%s\t%s\t%d\n", $1, $2, $3}' | sort)
}
list_links() { (cd "$1" && find . -path ./staging -prune -o -type l -printf '%P\t%l\n' | sort); }
# tile table without the verification date (it changes when DEST is verified)
tile_table() { "$IL" tiles "$1" | awk 'NR > 1 && NF >= 9 {print $1, $2, $3, $4, $5, $6, $7, $8, $9}'; }

prepare() {
    local src kit
    src=$(realpath "$1"); kit=$2
    [ -f "$src/LIBRARY.yaml" ] || die "$src is not a library root"
    mkdir -p "$kit"; kit=$(realpath "$kit")
    [ -e "$kit/files.tsv" ] && die "$kit already prepared"
    say "listing $src"
    list_files "$src" > "$kit/files.tsv"
    list_links "$src" > "$kit/symlinks.tsv"
    say "SHA-256 of every non-imagery file"
    awk -F'\t' '$1 !~ /\.tif$/ {print $1}' "$kit/files.tsv" | (cd "$src" && xargs -r -d '\n' sha256sum) > "$kit/nontif.sha256"
    tile_table "$src" > "$kit/tiles.src.txt"
    echo "$src" > "$kit/source"
    awk -F'\t' '{n++; b += $2; if ($1 ~ /\.tif$/) t++} END {printf "%d files (%d imagery), %.3f TB\n", n, t, b / 1e12}' \
        "$kit/files.tsv" | tee "$kit/summary.txt"
    echo "$(wc -l < "$kit/symlinks.tsv") symlinks, $(wc -l < "$kit/nontif.sha256") non-imagery checksums, $(wc -l < "$kit/tiles.src.txt") tiles" | tee -a "$kit/summary.txt"
}

check() {
    local kit dest fail=0
    kit=$(realpath "$1"); dest=$(realpath "$2")
    [ -f "$kit/files.tsv" ] || die "$kit is not prepared"
    [ -f "$dest/LIBRARY.yaml" ] || die "$dest has no LIBRARY.yaml (copy incomplete?)"
    rm -f "$kit/CHECK_PASSED"

    say "1/5 files, sizes, mtimes, symlinks"
    list_files "$dest" > "$kit/files.dest.tsv"
    list_links "$dest" > "$kit/symlinks.dest.tsv"
    # Files check itself rewrites (verify records, catalog) are compared on the first check only:
    # a re-check after a failed or interrupted check would otherwise always fail on them.
    local src_list="$kit/files.tsv" dest_list="$kit/files.dest.tsv" sums="$kit/nontif.sha256"
    if [ -e "$kit/CHECK_STARTED" ]; then
        echo "(re-check: verify records and catalog were rewritten by the previous check; not compared)"
        grep -v -P '^(catalog/|admin/verified/|admin/verification\.jsonl\t)' "$kit/files.tsv" > "$kit/files.cmp.tsv" || true
        grep -v -P '^(catalog/|admin/verified/|admin/verification\.jsonl\t)' "$kit/files.dest.tsv" > "$kit/files.dest.cmp.tsv" || true
        grep -v -P '^\S+  (catalog/|admin/verified/|admin/verification\.jsonl$)' "$kit/nontif.sha256" > "$kit/nontif.cmp.sha256" || true
        src_list="$kit/files.cmp.tsv"; dest_list="$kit/files.dest.cmp.tsv"; sums="$kit/nontif.cmp.sha256"
    fi
    # paths only in SRC (missing or different at DEST), paths only at DEST (extra or different)
    comm -23 "$src_list" "$dest_list" > "$kit/files.missing_or_different"
    comm -13 "$src_list" "$dest_list" > "$kit/files.extra_or_different"
    # a previous check's catalog build is the only thing allowed to be extra
    grep -v -P '^catalog/builds/' "$kit/files.extra_or_different" > "$kit/files.extra_unexpected" || true
    # temp files of an rsync that was killed (.<name>.XXXXXX); the rerun copied the file again
    grep -P '(^|/)\.[^/]+\.[A-Za-z0-9]{6}\t' "$kit/files.extra_unexpected" | cut -f1 > "$kit/files.rsync_temp" || true
    if [ -s "$kit/files.rsync_temp" ]; then
        echo "$(wc -l < "$kit/files.rsync_temp") temp file(s) left by an interrupted rsync. After the rsync command has"
        echo "completed without errors, remove them, then check again:"
        echo "  cd '$dest' && xargs -d '\\n' rm -f -- < '$kit/files.rsync_temp'"
        die "rsync temp files present"
    fi
    local m e l
    m=$(wc -l < "$kit/files.missing_or_different"); e=$(wc -l < "$kit/files.extra_unexpected")
    l=$(comm -3 "$kit/symlinks.tsv" "$kit/symlinks.dest.tsv" | { grep -v -P '^\t?catalog/' || true; } | wc -l)
    echo "missing or different: $m   unexpected extra: $e   symlink differences (outside catalog/): $l"
    [ "$m" -eq 0 ] && [ "$e" -eq 0 ] && [ "$l" -eq 0 ] || {
        head -5 "$kit/files.missing_or_different" "$kit/files.extra_unexpected"
        die "file lists differ: rerun the rsync command, then check again"; }

    say "2/5 SHA-256 of $(wc -l < "$sums") non-imagery files"
    (cd "$dest" && sha256sum -c --quiet "$sums") || die "non-imagery checksum mismatch (see above)"
    echo "OK"

    touch "$kit/CHECK_STARTED"
    say "3/5 full imagery verify (every file against the SHA-256 recorded when it was written)"
    local started; started=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
    "$IL" verify "$dest" 2>&1 | tee "$kit/verify.dest.log" || fail=1
    grep -q ': OK' "$kit/verify.dest.log" || fail=1
    grep -v -E ': OK|^$' "$kit/verify.dest.log" | grep -q . && fail=1
    if [ $fail -ne 0 ]; then
        # files that failed (verify records up to 10 per tile), as paths under DEST
        python3 -I - "$dest/admin/verification.jsonl" "$started" > "$kit/files.bad" <<'PY'
import json, sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    if r.get("started", "") >= sys.argv[2] and not r.get("ok"):
        if r.get("problems", 0) > len(r.get("examples", [])):
            print(f"# {r['path']}: {r['problems']} problems, only {len(r['examples'])} listed: re-copy the whole tile", file=sys.stderr)
        for ex in r.get("examples", []):
            print(f"{r['path']}/{ex.split(': ', 1)[1]}")
PY
        echo "failed files ($kit/files.bad):"; sed 's/^/  /' "$kit/files.bad"
        echo "To repair: delete them at DEST, rerun the rsync command (it copies them again), then check again:"
        echo "  cd '$dest' && xargs -d '\\n' rm -f -- < '$kit/files.bad'"
        die "verify reported problems: $kit/verify.dest.log"
    fi

    say "4/5 catalog"
    "$IL" catalog "$dest" | tee "$kit/catalog.dest.log"

    say "5/5 per-tile counts equal the source"
    tile_table "$dest" > "$kit/tiles.dest.txt"
    # the verification state may improve (unverified -> verified); everything else must match
    if diff <(cut -d' ' -f1-8 "$kit/tiles.src.txt") <(cut -d' ' -f1-8 "$kit/tiles.dest.txt"); then
        echo "OK: $(wc -l < "$kit/tiles.dest.txt") tiles identical"
    else
        die "tile tables differ"
    fi
    # the verification column is last; the acquisition column can hold spaces ("incomplete (1 failed)")
    "$IL" tiles "$dest" | awk 'NR > 1 && NF >= 9 {n++; if ($0 !~ / verified [0-9]{4}-[0-9]{2}-[0-9]{2} *$/) bad++}
        END {if (bad || !n) {print bad " tile(s) not verified"; exit 1}}' || die "not every tile is verified"
    echo "$dest" > "$kit/CHECK_PASSED"
    say "CHECK PASSED: $dest"
}

promote() {
    local kit staged final e
    kit=$(realpath "$1"); final=$(realpath "$2")
    [ -f "$kit/CHECK_PASSED" ] || die "run check first"
    staged=$(cat "$kit/CHECK_PASSED")
    [ -d "$final" ] || die "$final does not exist"
    [ "$(stat -c %d "$staged")" = "$(stat -c %d "$final")" ] || die "$staged and $final are on different filesystems"
    [ -e "$final/LIBRARY.yaml" ] && die "$final already holds a library"
    local entries
    entries=$(cd "$staged" && ls -A | grep -v -x -e staging -e LIBRARY.yaml; echo LIBRARY.yaml)
    for e in $entries; do [ -e "$final/$e" ] && die "$final/$e already exists"; done
    say "moving into $final (renames, LIBRARY.yaml last)"
    for e in $entries; do mv -n -T "$staged/$e" "$final/$e"; [ -e "$staged/$e" ] && die "could not move $e"; echo "  $e"; done
    [ -d "$staged/staging" ] && rmdir "$staged/staging"
    rmdir "$staged"
    say "catalog at the permanent root"
    "$IL" catalog "$final"
    if diff <(cut -d' ' -f1-8 "$kit/tiles.src.txt") <(tile_table "$final" | cut -d' ' -f1-8) > /dev/null; then
        say "PROMOTED: $final ($(wc -l < "$kit/tiles.src.txt") tiles, identical to the source)"
    else
        die "tile table at $final differs from the source"
    fi
}

case "${1:-}" in
    prepare) [ $# -eq 3 ] || die "usage: $0 prepare SRC KIT"; prepare "$2" "$3" ;;
    check)   [ $# -eq 3 ] || die "usage: $0 check KIT DEST"; check "$2" "$3" ;;
    promote) [ $# -eq 3 ] || die "usage: $0 promote KIT FINAL"; promote "$2" "$3" ;;
    *) sed -n '2,15p' "$0"; exit 2 ;;
esac
