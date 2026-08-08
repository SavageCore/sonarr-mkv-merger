#!/usr/bin/env bash
# qBittorrent completion hook for the Sonarr mkv merger.
# Configure qBittorrent AutoRun as:
#   bash /opt/sonarr-mkv-merger/qbt_complete.sh "%I" "%F"
#
# %I = infoHash, %F = content path (folder for multi-file, file for single-file).
#
# NOTE: This public build only runs the mkv merger. If you use cross-seed, add
# your own announce call (curl -XPOST "https://cross-seed:2468/api/webhook?apikey=..."
# --data-urlencode "infoHash=$INFO_HASH") BEFORE the merger block below.

set -u

INFO_HASH="${1:-}"
CONTENT="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGER="${MKV_MERGER_SCRIPT:-$SCRIPT_DIR/sonarr_mkv_merger.py}"

log() {
    printf '%s %s\n' "$(date '+%F %T')" "$*" >> /var/log/qbt_complete.log
}

if [[ -z "$CONTENT" ]]; then
    log "qbt_complete: missing content path, skipping"
    exit 0
fi

if [[ -f "$CONTENT" ]]; then
    TARGET="$(dirname "$CONTENT")"
else
    TARGET="$CONTENT"
fi

if [[ -d "$TARGET" ]]; then
    python3 "$MERGER" --dir "$TARGET" >> /var/log/qbt_complete.log 2>&1
    log "merger: processed $TARGET (exit $?)"
fi
