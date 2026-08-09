# sonarr-mkv-merger

Merge split TV episode releases (e.g. `MASH.S04E01a` + `MASH.S04E01b`)
losslessly into a single episode file with mkvmerge, keep the original parts for seeding, and
trigger Sonarr to import the merged result.

## Requirements

- **Python 3.8+** (any distribution, standard library only - no `pip` install needed)
- **mkvtoolnix** (provides the `mkvmerge` binary)

```sh
# Debian / Ubuntu
sudo apt install python3 mkvtoolnix

# Fedora
sudo dnf install python3 mkvtoolnix

# Arch
sudo pacman -S python mkvtoolnix
```

## Quick start (≈5 minutes)

```sh
git clone https://github.com/SavageCore/sonarr-mkv-merger.git
cd sonarr-mkv-merger
cp config.conf.example config.conf
```

Edit `config.conf` and set at least:

```ini
sonarr_url   = http://127.0.0.1:8989
sonarr_apikey = <your-sonarr-api-key>
```

Check it detects without making changes:

```sh
python3 sonarr_mkv_merger.py --scan /path/to/downloads --dry-run
```

Then run it for real:

```sh
python3 sonarr_mkv_merger.py --scan /path/to/downloads
```

> `config.conf` is picked up automatically if it lives next to the script or in
> `/etc/sonarr-mkv-merger/config.conf`. It is gitignored - only
> `config.conf.example` is committed.

## Configuration

Settings are read from three sources, in order of precedence:

**CLI flag → environment variable → `config.conf` → built-in default.**

| `config.conf` key | Environment variable | CLI flag | Default |
|---|---|---|---|
| `mkvmerge_path` | `MKVMERGE_PATH` | `--mkvmerge` | `mkvmerge` (search PATH) |
| `log_level` | `LOG_LEVEL` | `--log-level` | `info` |
| `dry_run` | `DRY_RUN` | `--dry-run` / `--no-dry-run` | `false` |
| `cleanup_mode` | `CLEANUP_MODE` | `--cleanup` | `keep` |
| `backup_dir` | `BACKUP_DIR` | `--backup-dir` | `.merged-parts` |
| `max_parts` | `MAX_PARTS` | `--max-parts` | `9` |
| `min_part_size` | `MIN_PART_SIZE` | `--min-part-size` | `10485760` |
| `sonarr_url` | `SONARR_URL` | `--sonarr-url` | *(none)* |
| `sonarr_apikey` | `SONARR_APIKEY` | `--sonarr-apikey` | *(none)* |
| `sonarr_trigger` | `SONARR_TRIGGER` | `--no-sonarr-trigger` | `true` |
| `check_tracks` | `CHECK_TRACKS` | `--check-tracks` | `true` |

Notes:

- `cleanup_mode`: `keep` (default) leaves originals for seeding, `move` moves
  them into `backup_dir`, `delete` removes them - all only **after** a verified
  merge.
- `min_part_size`: a part smaller than this (bytes) is treated as corrupt and the
  group is skipped.
- `check_tracks`: runs `mkvmerge -i` on every part and requires identical track
  layouts before merging (desync protection).
- The Sonarr trigger is skipped if `sonarr_url` or `sonarr_apikey` is empty, or
  if `sonarr_trigger = false`.

## Usage

### qBittorrent AutoRun

In **qBittorrent → Options → Downloads → Run external program on torrent
completion**, set:

```
bash /path/to/qbt_complete.sh "%I" "%F"
```

- `%I` = info hash, `%F` = content path.
- The bundled `qbt_complete.sh` resolves `%F` to a folder and runs the merger.
  Logs go to `/var/log/qbt_complete.log`.
- Or call the merger directly:

```
python3 /path/to/sonarr_mkv_merger.py --dir "%F"
```

### Sonarr Custom Script (Connect)

1. **Sonarr → Settings → Connect → + → Custom Script**.
2. Path: `/path/to/sonarr_mkv_merger.py`.
3. Event: **On File Import**
4. No arguments needed: the script auto-detects the `Sonarr_EpisodeFile_SourceFolder`
   / `Sonarr_EpisodeFile_Path` environment variables and scans that folder.

### Manual scan / re-run

Original parts stay in the download folder, so you can re-run any time - it skips
episodes whose merged output already exists:

```sh
python3 sonarr_mkv_merger.py --scan /mnt/media/downloads
python3 sonarr_mkv_merger.py --dir "/mnt/media/downloads/MASH.S04.REPACK.1080p.AMZN.WEB-DL.DD+2.0.H.264-AJP69"
python3 sonarr_mkv_merger.py --dry-run --scan /mnt/media/downloads
```

## How Sonarr import works (`importMode: copy` + hardlinks)

After a successful merge the script calls Sonarr's `DownloadedEpisodesScan` API
with `importMode: "copy"`:

```json
{ "name": "DownloadedEpisodesScan", "path": "/downloads/...", "importMode": "copy" }
```

This imports the merged file into your library as a hardlink pointing to the
same on-disk data, leaving the original split parts in the download folder so
the torrent/NZB keeps seeding without duplicating disk space.

## Behavior & safety

- Detects `SxxEyy[a-z]`, `Part 1/2`, and `CD1/2` markers.
- Groups only parts of the **exact same episode base** and requires all
  consecutive parts to be present; non-contiguous groups are skipped.
- Refuses to merge if any part is below `min_part_size` or if parts fail the
  `mkvmerge -i` track-layout check (possible desync).
- Merge command: `mkvmerge -o OUT PART1 + PART2` - zero re-encode.
- Output is written to a `.part` temp file then atomically renamed; originals are
  only moved/deleted **after** the output exists and is non-empty.
- Idempotent: folders whose merged file already exists are skipped.

## Integrations

### `qbt_complete.sh` - combined cross-seed + merger hook

`qbt_complete.sh` is a qBittorrent AutoRun wrapper that runs **both** a
[cross-seed](https://github.com/cross-seed/cross-seed) announce and the merger
in one completion hook. It only makes sense for users who are cross-seeding too
- if you are not, call the merger directly (see the qBittorrent section above).

The hook announces to cross-seed **first** (before the merger touches the
folder), then runs the merger. Configure it in **qBittorrent → Options →
Downloads → Run external program on torrent completion**:

```
CROSS_SEED_URL="http://cross-seed:2468/api/webhook" \
CROSS_SEED_KEY="your-cross-seed-apikey" \
bash /path/to/qbt_complete.sh "%I" "%F"
```

- `%I` = info hash (used for the announce), `%F` = content path (used for the
  merger).
- `CROSS_SEED_URL` / `CROSS_SEED_KEY` are **optional**. If both are set and an
  info hash is present, the announce runs; if either is unset, the announce is
  skipped and **only the merger runs**.
- The merger call respects the `MKV_MERGER_SCRIPT` env var, which overrides the
  default `sonarr_mkv_merger.py` path next to the hook.
- Logs go to `/var/log/qbt_complete.log`.

## License

[MIT](LICENSE) © 2026 SavageCore
