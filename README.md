# sonarr-mkv-merger

Merge split/parted TV episode releases (e.g. `Show.S04E01a` + `Show.S04E01b`)
losslessly into a single episode file, keep the original parts for seeding, and
trigger Sonarr to import the merged result.

Zero external Python dependencies - pure standard library.

## What it does and why

Some scene releases split an episode across multiple files with suffixes like
`a`/`b`, `Part 1`/`Part 2`, or `CD1`/`CD2`:

```
Show.S04E01a.mkv   +   Show.S04E01b.mkv   →   Show.S04E01.mkv
```

Sonarr cannot import these as a single episode. This tool:

1. Detects split groups in a download folder.
2. Merges them **losslessly** with `mkvmerge` (no re-encoding).
3. Leaves the original parts **untouched** by default, so the torrent/NZB keeps
   seeding.
4. Triggers Sonarr to import the new merged file.

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
3. Event: **On Download** (not "On Import Complete" - that event only fires once
   a whole batch finishes importing, which a split release never does).
4. No arguments needed: the script auto-detects the `Sonarr_EpisodeFile_SourceFolder`
   / `Sonarr_EpisodeFile_Path` environment variables and scans that folder.

### SABnzbd

In **SABnzbd → Switches → Scripts**, add a script that calls the merger with the
downloaded path (`$1`):

```
python3 /path/to/sonarr_mkv_merger.py "$1"
```

Enable the script and assign it to your categories.

### Manual scan / re-run

Original parts stay in the download folder, so you can re-run any time - it skips
episodes whose merged output already exists:

```sh
python3 sonarr_mkv_merger.py --scan /mnt/media/downloads
python3 sonarr_mkv_merger.py --dir "/mnt/media/downloads/Show.S04E01.1080p"
python3 sonarr_mkv_merger.py --dry-run --scan /mnt/media/downloads
```

## About the `importMode: copy` / hardlink behavior

**This is the most important detail in the whole project.**

After merging, the script triggers Sonarr's `DownloadedEpisodesScan` API command
**with `importMode: "copy"`** - not the default:

```json
{ "name": "DownloadedEpisodesScan", "path": "/downloads/...", "importMode": "copy" }
```

Why it matters:

- The default (no `importMode`, i.e. `move`) makes Sonarr **move** the file into
  the library, **stripping it out of the seeding torrent folder** and breaking
  your ratio.
- With `importMode: "copy"` **and** Sonarr's `copyUsingHardlinks` setting enabled,
  Sonarr creates a **hardlink** in the library instead. Both paths point to the
  same data on disk - the merged file is importable while the original parts
  remain in place for seeding.

**Prerequisite:** in Sonarr, enable **Settings → Media Management → Importing →
Copy using Hardlinks**. Without this, `copy` still duplicates the data rather
than hardlinking, which wastes space.

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

- **`qbt_complete.sh`** - optional qBittorrent AutoRun wrapper (see above). The
  public build runs only the merger. If you use [cross-seed](https://github.com/cross-seed/cross-seed),
  add your own announce call before the merger block:
  `curl -sS -XPOST "http://<cross-seed-host>:2468/api/webhook?apikey=<key>" --data-urlencode "infoHash=$INFO_HASH"`.

## License

[MIT](LICENSE) © 2026 SavageCore
