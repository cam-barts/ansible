#!/bin/bash
# One-off cleanup of paths blocking rsnapshot native_cp_al rotation.
# Run on gastown with: sudo bash cleanup-snapshots.sh           (dry-run)
# Then:                sudo bash cleanup-snapshots.sh --apply
# After successful apply: this script is obsolete — backup_targets
# excludes prevent these paths from being re-introduced.
#
# Three classes of cleanup:
#
# 1. Mode-0000 broken slots. A rogue `rsync -av --delete` (no --link-dest)
#    driven by synoschedtask id=4 has been running hourly since ~May 13
#    and leaving in-progress destination directories at mode d---------.
#    These are NOT real rsnapshot snapshots — they're failed raw-rsync
#    copies with no hardlinked content. Safe to delete; the real-data
#    slots (hourly.0 and hourly.5+) are intact.
#
# 2. warrig symlink-loop paths under each snapshot's warrig/home/.
#
# 3. wellerman .venv directories (regeneratable noise; the excludes file
#    keeps them out going forward).

set -euo pipefail

SNAPSHOT_ROOT="/volume1/NetBackup/pi_rsnapshots"

APPLY=0
if [ "${1:-}" = "--apply" ]; then
    APPLY=1
fi

if [ ! -d "$SNAPSHOT_ROOT" ]; then
    echo "ERROR: snapshot root not found: $SNAPSHOT_ROOT" >&2
    exit 1
fi

if [ "$APPLY" -eq 1 ]; then
    echo "=== MODE: APPLY (deletions are real) ==="
else
    echo "=== MODE: DRY-RUN (pass --apply to actually delete) ==="
fi
echo "Snapshot root: $SNAPSHOT_ROOT"
echo

echo "--- df -h /volume1 (before) ---"
df -h /volume1
echo

# size_of PATH — print human-readable size, "0" if missing, "?" if du
# can't compute in time. Mode-0000 broken slots can take minutes to
# traverse; bound the cost so the script never hangs.
size_of() {
    if [ ! -e "$1" ]; then
        echo "0"
        return
    fi
    local out
    out=$(timeout 30s du -sh "$1" 2>/dev/null | awk '{print $1}' || true)
    if [ -n "$out" ]; then
        echo "$out"
    else
        echo "?"
    fi
}

# remove_path PATH LABEL — log + rm -rf (or just log in dry-run).
remove_path() {
    local p="$1"
    local label="$2"
    if [ ! -e "$p" ]; then
        return 0
    fi
    local sz
    sz=$(size_of "$p")
    if [ "$APPLY" -eq 1 ]; then
        echo "DELETE [$label] $p ($sz)"
        rm -rf -- "$p"
    else
        echo "WOULD DELETE [$label] $p ($sz)"
    fi
}

shopt -s nullglob

# Pass 1: delete mode-0000 broken slots before walking into them. These are
# failed in-progress raw-rsync copies (synoschedtask id=4 rogue rsync), not
# real rsnapshot snapshots — see header. Removing them first means the
# warrig/wellerman pass below doesn't trip on dirs it can't traverse.
echo "--- pass 1: mode-0000 broken slots ---"
for snap_dir in "$SNAPSHOT_ROOT"/hourly.* "$SNAPSHOT_ROOT"/daily.* "$SNAPSHOT_ROOT"/weekly.* "$SNAPSHOT_ROOT"/monthly.*; do
    [ -d "$snap_dir" ] || continue
    mode=$(stat -c '%a' "$snap_dir" 2>/dev/null || echo "")
    if [ "$mode" = "0" ]; then
        remove_path "$snap_dir" "broken-slot"
    fi
done
echo

# Pass 2: walk every remaining snapshot tier for warrig/wellerman cleanup.
# Glob handles missing tiers gracefully (nullglob).
echo "--- pass 2: warrig loops + wellerman .venvs ---"
for snap in "$SNAPSHOT_ROOT"/hourly.* "$SNAPSHOT_ROOT"/daily.* "$SNAPSHOT_ROOT"/weekly.* "$SNAPSHOT_ROOT"/monthly.*; do
    [ -d "$snap" ] || continue
    echo "--- snapshot: $snap ---"

    # warrig: symlink-loop paths.
    remove_path "$snap/warrig/home/home" "warrig-loop"
    remove_path "$snap/warrig/home/nux/home" "warrig-loop"

    # wellerman: every .venv anywhere below the wellerman tree.
    if [ -d "$snap/wellerman" ]; then
        while IFS= read -r -d '' venv; do
            remove_path "$venv" "wellerman-venv"
        done < <(find "$snap/wellerman" -type d -name .venv -prune -print0)
    fi
done

echo
echo "--- df -h /volume1 (after) ---"
df -h /volume1

echo
if [ "$APPLY" -eq 1 ]; then
    echo "Cleanup complete."
else
    echo "Dry-run complete. Re-run with --apply to actually delete."
fi
