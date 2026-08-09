#!/usr/bin/env bash
# qBittorrent completion hook: cross-seed announce + Sonarr mkv merger.
# Configure qBittorrent AutoRun as:
#   CROSS_SEED_URL="http://cross-seed:2468/api/webhook" \
#   CROSS_SEED_KEY="your-key" \
#   bash /path/to/qbt_complete.sh "%I" "%F"
#
# %I = infoHash, %F = content path (folder for multi-file, file for single-file).
#
# CROSS_SEED_URL and CROSS_SEED_KEY are optional. If both are set and an info
# hash is present, the hook announces to cross-seed BEFORE running the merger.
# If either is unset, the announce is skipped and only the merger runs.

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

# 1) cross-seed announce (before the merger modifies the folder)
if [[ -n "$INFO_HASH" && -n "${CROSS_SEED_URL:-}" && -n "${CROSS_SEED_KEY:-}" ]]; then
    if command -v curl >/dev/null 2>&1; then
        curl -sS -m 20 -XPOST "$CROSS_SEED_URL?apikey=$CROSS_SEED_KEY" \
            --data-urlencode "infoHash=$INFO_HASH" >> /var/log/qbt_complete.log 2>&1 \
            && log "cross-seed: announced $INFO_HASH" \
            || log "cross-seed: failed for $INFO_HASH (non-fatal)"
    else
        log "curl not found, skipping cross-seed announce"
    fi
fi

# 2) mkv merger
if [[ -f "$CONTENT" ]]; then
    TARGET="$(dirname "$CONTENT")"
else
    TARGET="$CONTENT"
fi

if [[ -d "$TARGET" ]]; then
    python3 "$MERGER" --dir "$TARGET" >> /var/log/qbt_complete.log 2>&1
    log "merger: processed $TARGET (exit $?)"
fi
