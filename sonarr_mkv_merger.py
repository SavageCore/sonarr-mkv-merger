#!/usr/bin/env python3
"""Sonarr Multi-Part Episode Auto-Merger.

Detects split episode releases (SxxEyy[a-z], Part/CD tokens), merges the parts
losslessly with mkvmerge into a single file, and leaves the original parts
untouched so a torrent/NZB can keep seeding.

Trigger modes (any):
  * qBittorrent external program:   python3 sonarr_mkv_merger.py --dir "%F"
  * SABnzbd post-processing:        python3 sonarr_mkv_merger.py "$1"
  * Sonarr Custom Script connect:   runs automatically, reads Sonarr_* env vars
  * Manual re-run / rescan:         python3 sonarr_mkv_merger.py --scan /media/downloads

Uses only the Python standard library.
"""

import argparse
import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("sonarr_mkv_merger")

EPISODE_RE = re.compile(r"(?P<ep>[Ss]\d{1,2}[Ee]\d{2,3})")
LETTER_MARKER_RE = re.compile(r"(?P<ep>[Ss]\d{1,2}[Ee]\d{2,3})(?P<letter>[a-z])(?=\.|-|_|$)")
NUMBER_MARKER_RE = re.compile(r"\.?(?:Part|CD|Disc|Pt)\.?[\s_.-]*(\d+)", re.IGNORECASE)
TITLE_PART_TOKEN_RE = re.compile(r"\.(?:Part|CD|Disc|Pt)\.?\d+", re.IGNORECASE)
NUM_BEFORE_RES_RE = re.compile(r"\.\d(?=\.\d{3,4}p)")

DEFAULT_MAX_PARTS = 9
DEFAULT_MIN_PART_SIZE = 10 * 1024 * 1024


def parse_args(argv):
    p = argparse.ArgumentParser(description="Merge split multi-part episodes losslessly.")
    p.add_argument("paths", nargs="*", help="Directory(ies) to process (SABnzbd passes a path here).")
    p.add_argument("--dir", action="append", default=[], help="Process one release folder. Repeatable.")
    p.add_argument("--scan", action="append", default=[], help="Recursively scan a root for release folders.")
    p.add_argument("--config", default=None, help="INI config file (optional).")
    p.add_argument("--dry-run", action="store_true", default=None, help="Detect and report, make no changes.")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Force real run (overrides config/env).")
    p.add_argument("--log-level", default=None, help="debug|info|warning|error")
    p.add_argument("--mkvmerge", default=None, help="Path to mkvmerge binary.")
    p.add_argument("--cleanup", default=None, choices=["keep", "move", "delete"],
                   help="What to do with original parts after a verified merge (default: keep).")
    p.add_argument("--backup-dir", default=None,
                   help="For cleanup=move: subfolder to move parts into (default: '.merged-parts').")
    p.add_argument("--max-parts", type=int, default=None, help="Maximum parts per episode group.")
    p.add_argument("--min-part-size", type=int, default=None, help="Minimum part size in bytes to trust a part.")
    p.add_argument("--sonarr-url", default=None, help="Sonarr base URL e.g. http://127.0.0.1:8989")
    p.add_argument("--sonarr-apikey", default=None, help="Sonarr API key.")
    p.add_argument("--no-sonarr-trigger", action="store_true", default=None,
                   help="Disable the Sonarr DownloadedEpisodesScan trigger.")
    p.add_argument("--check-tracks", action="store_true", default=None,
                   help="Validate track layout of each part with mkvmerge -i before merging.")
    return p.parse_args(argv)


def load_config(cli):
    cfg = configparser.ConfigParser()
    cfg["defaults"] = {}
    if cli.config:
        cfg.read(cli.config)
    cfg.read(["/etc/sonarr-mkv-merger/config.conf",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.conf")])

    env = os.environ
    defaults = cfg["defaults"] if cfg.has_section("defaults") else {}

    def pick(cli_val, env_name, cfg_key, default):
        if cli_val is not None:
            return cli_val
        if env_name in env and env[env_name].strip() != "":
            return env[env_name]
        if cfg_key in defaults:
            return defaults[cfg_key]
        return default

    mkvmerge = pick(cli.mkvmerge, "MKVMERGE_PATH", "mkvmerge_path", "mkvmerge")
    log_level = pick(cli.log_level, "LOG_LEVEL", "log_level", "info").lower()

    dry_run = pick(cli.dry_run, "DRY_RUN", "dry_run", "false")
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("1", "true", "yes", "on")
    elif dry_run is None:
        dry_run = False

    cleanup = pick(cli.cleanup, "CLEANUP_MODE", "cleanup_mode", "keep").lower()
    backup_dir = pick(cli.backup_dir, "BACKUP_DIR", "backup_dir", ".merged-parts")
    max_parts = int(pick(cli.max_parts, "MAX_PARTS", "max_parts", DEFAULT_MAX_PARTS))
    min_part_size = int(pick(cli.min_part_size, "MIN_PART_SIZE", "min_part_size", DEFAULT_MIN_PART_SIZE))

    sonarr_url = pick(cli.sonarr_url, "SONARR_URL", "sonarr_url", None)
    sonarr_apikey = pick(cli.sonarr_apikey, "SONARR_APIKEY", "sonarr_apikey", None)

    no_trigger = pick(cli.no_sonarr_trigger, "SONARR_TRIGGER", "sonarr_trigger", "true")
    if isinstance(no_trigger, str):
        no_trigger = no_trigger.lower() in ("0", "false", "no", "off")
    elif no_trigger is None:
        no_trigger = False
    sonarr_trigger = not no_trigger

    check_tracks = pick(cli.check_tracks, "CHECK_TRACKS", "check_tracks", "true")
    if isinstance(check_tracks, str):
        check_tracks = check_tracks.lower() in ("1", "true", "yes", "on")
    elif check_tracks is None:
        check_tracks = True

    return {
        "mkvmerge": mkvmerge,
        "log_level": log_level,
        "dry_run": dry_run,
        "cleanup": cleanup,
        "backup_dir": backup_dir,
        "max_parts": max_parts,
        "min_part_size": min_part_size,
        "sonarr_url": sonarr_url,
        "sonarr_apikey": sonarr_apikey,
        "sonarr_trigger": sonarr_trigger,
        "check_tracks": check_tracks,
    }


def sonarr_env_dir():
    event = os.environ.get("Sonarr_EventType")
    if not event:
        return None
    source_folder = os.environ.get("Sonarr_EpisodeFile_SourceFolder")
    if source_folder and os.path.isdir(source_folder):
        return source_folder
    path = os.environ.get("Sonarr_EpisodeFile_Path")
    if path:
        parent = os.path.dirname(path)
        if os.path.isdir(parent):
            return parent
    return None


def gather_targets(cfg, cli):
    targets = []
    targets.extend(cli.dir)
    targets.extend(cli.paths)
    env_dir = sonarr_env_dir()
    if env_dir:
        LOG.info("Sonarr Custom Script detected; processing source folder: %s", env_dir)
        targets.append(env_dir)
    for root in cli.scan:
        if not os.path.isdir(root):
            LOG.warning("Scan root does not exist: %s", root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if any(f.lower().endswith((".mkv", ".mp4", ".avi", ".ts")) for f in filenames):
                targets.append(dirpath)
    seen = set()
    result = []
    for t in targets:
        t = os.path.abspath(t)
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def parse_part(filename):
    stem = Path(filename).stem
    m = LETTER_MARKER_RE.search(stem)
    if m:
        episode = m.group("ep")
        letter = m.group("letter")
        base = stem[: m.start("letter")] + stem[m.end("letter"):]
        base = normalize_base(base)
        return {"episode": episode, "marker_type": "letter", "marker": letter,
                "sort_key": (0, ord(letter)), "base": base}
    m = EPISODE_RE.search(stem)
    if m:
        episode = m.group("ep")
        tail = stem[m.end("ep"):]
        nm = NUMBER_MARKER_RE.search(tail)
        if nm:
            number = int(nm.group(1))
            base = stem[: m.end("ep")] + tail[: nm.start()] + tail[nm.end():]
            base = normalize_base(base)
            return {"episode": episode, "marker_type": "number", "marker": number,
                    "sort_key": (1, number), "base": base}
    return None


def normalize_base(base):
    base = TITLE_PART_TOKEN_RE.sub("", base)
    base = NUM_BEFORE_RES_RE.sub("", base)
    base = re.sub(r"[ _]+", ".", base).strip(".")
    return base


def find_split_groups(directory, cfg):
    parts = []
    for entry in os.scandir(directory):
        if entry.is_file():
            low = entry.name.lower()
            if not low.endswith((".mkv", ".mp4", ".avi", ".ts")):
                continue
            if entry.name.startswith("."):
                continue
            parsed = parse_part(entry.name)
            if parsed:
                size = entry.stat().st_size
                parsed["path"] = entry.path
                parsed["name"] = entry.name
                parsed["size"] = size
                parts.append(parsed)
    if not parts:
        return []

    groups = {}
    for part in parts:
        groups.setdefault(part["base"], []).append(part)

    result = []
    for base, members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda p: p["sort_key"])
        keys = [p["sort_key"] for p in members]
        if not contiguous(keys):
            LOG.warning("Non-contiguous parts in group %r - skipping (safety).", base)
            continue
        result.append({"base": base, "episode": members[0]["episode"], "members": members})
    return result


def contiguous(keys):
    if all(k[0] == 0 for k in keys):
        letters = [k[1] for k in keys]
        return letters == list(range(letters[0], letters[-1] + 1))
    if all(k[0] == 1 for k in keys):
        nums = [k[1] for k in keys]
        return nums == list(range(nums[0], nums[-1] + 1))
    return False


def mkvmerge_info(mkvmerge, path):
    try:
        proc = subprocess.run([mkvmerge, "-i", "-J", path], capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None


def track_layout(info):
    if not info or "tracks" not in info:
        return None
    return tuple((t["type"], t.get("codec")) for t in info["tracks"])


def parts_sane(group, cfg):
    for part in group["members"]:
        if part["size"] < cfg["min_part_size"]:
            LOG.error("Part too small, refusing to merge: %s (%d bytes)", part["name"], part["size"])
            return False
    if cfg["check_tracks"]:
        layouts = []
        for part in group["members"]:
            info = mkvmerge_info(cfg["mkvmerge"], part["path"])
            if info is None:
                LOG.error("Cannot read part with mkvmerge -i, refusing to merge: %s", part["name"])
                return False
            layouts.append(track_layout(info))
        if len(set(layouts)) != 1:
            LOG.error("Parts have different track layouts - refusing to merge %r (possible desync).", group["base"])
            return False
    return True


def merge_parts(group, directory, cfg):
    output_path = os.path.join(directory, group["base"] + ".mkv")
    if os.path.exists(output_path):
        LOG.info("Merged output already exists, skipping: %s", output_path)
        return None
    if os.path.exists(output_path + ".part"):
        LOG.warning("Stale temp file found, removing: %s", output_path + ".part")
        os.remove(output_path + ".part")

    cmd = [cfg["mkvmerge"], "-o", output_path + ".part"]
    for i, part in enumerate(group["members"]):
        if i > 0:
            cmd.append("+")
        cmd.append(part["path"])

    LOG.info("Merging %s -> %s", " + ".join(p["name"] for p in group["members"]), os.path.basename(output_path))
    if cfg["dry_run"]:
        return None

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        LOG.error("Failed to launch mkvmerge (%s): %s", cfg["mkvmerge"], exc)
        return None

    if proc.returncode != 0:
        LOG.error("mkvmerge failed (exit %s):\n%s", proc.returncode, (proc.stderr or proc.stdout)[-4000:])
        if os.path.exists(output_path + ".part"):
            os.remove(output_path + ".part")
        return None

    if not os.path.exists(output_path + ".part"):
        LOG.error("mkvmerge claimed success but produced no output - refusing to proceed.")
        return None

    out_size = os.path.getsize(output_path + ".part")
    if out_size <= 0:
        LOG.error("Merged file is empty (0 bytes) - refusing to proceed.")
        os.remove(output_path + ".part")
        return None

    os.replace(output_path + ".part", output_path)
    LOG.info("Merge verified: %s (%d bytes)", output_path, out_size)
    cleanup_parts(group, directory, cfg)
    return output_path


def cleanup_parts(group, directory, cfg):
    if cfg["dry_run"]:
        return
    mode = cfg["cleanup"]
    if mode == "keep":
        LOG.info("Cleanup=keep - original parts left untouched (seeding intact).")
        return
    if mode == "move":
        dest = os.path.join(directory, cfg["backup_dir"])
        os.makedirs(dest, exist_ok=True)
        for part in group["members"]:
            target = os.path.join(dest, part["name"])
            if os.path.exists(target):
                os.remove(target)
            shutil.move(part["path"], target)
            LOG.info("Moved original part to %s", target)
    elif mode == "delete":
        for part in group["members"]:
            os.remove(part["path"])
            LOG.info("Deleted original part %s", part["name"])


def trigger_sonarr(cfg, directory):
    if not cfg["sonarr_trigger"]:
        return
    url = cfg["sonarr_url"]
    apikey = cfg["sonarr_apikey"]
    if not url:
        LOG.info("No SONARR_URL configured - skipping Sonarr trigger.")
        return
    if not apikey:
        LOG.info("No SONARR_APIKEY configured - skipping Sonarr trigger.")
        return
    url = url.rstrip("/") + "/api/v3/command"
    # importMode "copy" (not the default "move"): the folder has no download-client
    # association, so a plain scan falls back to Move and empties the torrent folder.
    # With copyUsingHardlinks enabled Sonarr hardlinks into the library instead.
    payload = json.dumps({"name": "DownloadedEpisodesScan", "path": directory, "importMode": "copy"}).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("X-Api-Key", apikey)
    req.add_header("Content-Type", "application/json")
    LOG.info("Triggering Sonarr DownloadedEpisodesScan for %s", directory)
    if cfg["dry_run"]:
        return
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            LOG.info("Sonarr command accepted: HTTP %s", resp.status)
    except urllib.error.HTTPError as exc:
        LOG.error("Sonarr trigger failed (HTTP %s): %s", exc.code, exc.read().decode(errors="replace")[:500])
    except urllib.error.URLError as exc:
        LOG.error("Sonarr trigger failed: %s", exc)


def process_directory(directory, cfg):
    if not os.path.isdir(directory):
        LOG.warning("Not a directory, skipping: %s", directory)
        return 0
    groups = find_split_groups(directory, cfg)
    if not groups:
        return 0
    LOG.info("Directory %s: %d split release group(s) found.", directory, len(groups))
    merged = 0
    for group in groups:
        if not parts_sane(group, cfg):
            continue
        out = merge_parts(group, directory, cfg)
        if out:
            merged += 1
    if merged and cfg["sonarr_trigger"]:
        trigger_sonarr(cfg, directory)
    return merged


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    cli = parse_args(argv)
    cfg = load_config(cli)

    logging.basicConfig(
        level=getattr(logging, cfg["log_level"].upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        stream=sys.stderr,
    )

    if not cfg["dry_run"]:
        mkvmerge = shutil.which(cfg["mkvmerge"]) or cfg["mkvmerge"]
        if not os.path.isfile(mkvmerge):
            LOG.error("mkvmerge not found at %s - install mkvtoolnix or set MKVMERGE_PATH.", mkvmerge)
            return 2
        cfg["mkvmerge"] = mkvmerge

    if cfg["dry_run"]:
        LOG.warning("DRY RUN - no files will be created, moved, or deleted.")

    targets = gather_targets(cfg, cli)
    if not targets:
        LOG.info("Nothing to do. Pass --dir <folder>, --scan <root>, or a path argument.")
        return 0

    total = 0
    for directory in targets:
        total += process_directory(directory, cfg)
    LOG.info("Done. Merged %d episode group(s).", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
