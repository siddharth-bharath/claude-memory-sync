#!/usr/bin/env python3
"""claude-memory-sync — cross-device Claude Code memory + sessions + skills via Dropbox.

Two-way syncs, per run:
  1. Project memory + session transcripts for the project in the current working
     directory (or one passed via --project).
  2. Global ~/.claude/skills/ (unless --no-skills).

Identity is the project's folder *basename* (canonical name), not its absolute
path — so a repo that lives at ~/Projects/foo on Linux and D:\\Projects\\foo
on Windows is one logical entry. A registry at
Dropbox/claude-memory-sync/registry.json records each machine's local path.

Newest-mtime-wins per file. Never deletes. Before overwriting a differing file,
the older version is copied to Dropbox/claude-memory-sync/backups/<timestamp>/
so last-write-wins is recoverable.

Session transcripts (*.jsonl) are stored gzipped on Dropbox (*.jsonl.gz) and
decompressed back to *.jsonl on pull. Local Claude Code session files are
always raw .jsonl so /resume keeps working. Legacy raw .jsonl files left on
Dropbox by older versions are migrated to .jsonl.gz on next sync.

Dropbox is treated as a transport buffer, not an archive: sessions older
than SESSION_RETENTION_DAYS and old backup snapshots are pruned at the end
of every sync. The local copies on each machine are the canonical archive
and are never touched.

Usage:
  python sync.py                   # sync project in cwd + skills
  python sync.py --project /abs    # sync a different project
  python sync.py --all             # sync every registered project on this machine
  python sync.py --skills-only     # skip project work
  python sync.py --no-skills       # skip skills
  python sync.py --no-prune        # skip the prune-old-stuff pass at end
  python sync.py --dry-run
  python sync.py --list            # print registry
  python sync.py --export-bundle PATH
                                   # write a tar.gz of all local memory + sessions
                                   # + skills, organized by canonical name, for
                                   # bootstrapping a fresh machine with full history
                                   # (Dropbox only carries the rolling window).
"""
from __future__ import annotations
import argparse, gzip, json, os, shutil, socket, sys, tarfile, tempfile, time, filecmp
from pathlib import Path
from datetime import datetime

SYNC_SUBPATH = "claude-memory-sync"
PROJECTS_SUBDIR = "projects"
SKILLS_SUBDIR = "skills"
BACKUPS_SUBDIR = "backups"
REGISTRY_NAME = "registry.json"
LOG_DIR_NAME = "logs"

# Rolling-window retention. Dropbox is a transport buffer; the canonical copy
# of every session and memory file lives on each machine's local disk.
SESSION_RETENTION_DAYS = 90        # drop .jsonl.gz on Dropbox older than this
BACKUP_RETENTION_DAYS = 7          # keep all backups newer than this
BACKUP_RETENTION_MIN_RUNS = 10     # ...and always keep at least this many most-recent runs


def find_dropbox_root() -> Path:
    env = os.environ.get("CLAUDE_SYNC_DROPBOX")
    if env:
        p = Path(env)
        if p.exists():
            return p
    home = Path.home()
    candidates = [
        home / "Dropbox",
        Path("D:/Dropbox"),
        Path("C:/Dropbox"),
        home / "dropbox",
    ]
    for c in candidates:
        if c.exists():
            return c
    raise SystemExit(
        "Could not find Dropbox root. "
        "Set CLAUDE_SYNC_DROPBOX=/path/to/Dropbox."
    )


def path_to_key(path: Path) -> str:
    """Replicate Claude Code's folder-name transform for a project path."""
    s = str(path)
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    for ch in ("/", "\\", ":", "_", "."):
        s = s.replace(ch, "-")
    return s


def resolve_project_dir(abs_path: Path) -> Path:
    """Find ~/.claude/projects/<key> for a given absolute project path."""
    claude_projects = Path.home() / ".claude" / "projects"
    key = path_to_key(abs_path)
    exact = claude_projects / key
    if exact.exists():
        return exact
    if claude_projects.exists():
        key_lower = key.lower()
        for d in claude_projects.iterdir():
            if d.is_dir() and d.name.lower() == key_lower:
                return d
    return exact


def canonical_name(abs_path: Path) -> str:
    return abs_path.name


def backup_dir(dropbox_root: Path, run_stamp: str) -> Path:
    return dropbox_root / SYNC_SUBPATH / BACKUPS_SUBDIR / run_stamp


def copy_with_backup(src: Path, dst: Path, backups_root: Path, rel_for_backup: Path,
                     dry_run: bool, log: list):
    """Copy src over dst. If dst exists and differs, back it up first."""
    if dst.exists() and not filecmp.cmp(src, dst, shallow=False):
        bpath = backups_root / rel_for_backup
        log.append(f"    backup: {rel_for_backup}")
        if not dry_run:
            bpath.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bpath)
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def sync_pair(local: Path, remote: Path, label: str, backups_root: Path,
              dry_run: bool, log: list):
    """Two-way recursive sync. Never deletes. Backs up loser before overwrite."""
    if not local.exists() and not remote.exists():
        return
    local.mkdir(parents=True, exist_ok=True)
    remote.mkdir(parents=True, exist_ok=True)

    rels: set[Path] = set()
    for side in (local, remote):
        for p in side.rglob("*"):
            if p.is_file():
                rels.add(p.relative_to(side))

    for rel in sorted(rels):
        pl = local / rel
        pr = remote / rel
        if pl.exists() and pr.exists():
            mtl, mtr = pl.stat().st_mtime, pr.stat().st_mtime
            if mtl > mtr + 1:
                log.append(f"  [{label}] push {rel}")
                copy_with_backup(pl, pr, backups_root,
                                 Path(label) / "remote" / rel, dry_run, log)
            elif mtr > mtl + 1:
                log.append(f"  [{label}] pull {rel}")
                copy_with_backup(pr, pl, backups_root,
                                 Path(label) / "local" / rel, dry_run, log)
        elif pl.exists():
            log.append(f"  [{label}] push {rel} (new)")
            if not dry_run:
                pr.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pl, pr)
        else:
            log.append(f"  [{label}] pull {rel} (new)")
            if not dry_run:
                pl.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(pr, pl)


def gz_compress(src: Path, dst: Path):
    """Gzip src -> dst atomically; preserve src's mtime on dst."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    src_mtime = src.stat().st_mtime
    with src.open("rb") as f_in, gzip.GzipFile(
            filename=tmp, mode="wb", compresslevel=6, mtime=src_mtime) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.utime(tmp, (src_mtime, src_mtime))
    tmp.replace(dst)


def gz_decompress(src: Path, dst: Path):
    """Gunzip src -> dst atomically; preserve src's mtime on dst."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    src_mtime = src.stat().st_mtime
    with gzip.open(src, "rb") as f_in, tmp.open("wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.utime(tmp, (src_mtime, src_mtime))
    tmp.replace(dst)


def sync_jsonl(local_root: Path, remote_sessions: Path, backups_root: Path,
               dry_run: bool, log: list):
    """Sync *.jsonl at the project root only (not recursive).

    Local stores raw .jsonl (Claude Code reads these directly for /resume).
    Remote stores .jsonl.gz to save Dropbox space (sessions compress ~5–10x).
    Legacy raw .jsonl files on remote are migrated to .jsonl.gz on first run.
    """
    remote_sessions.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)

    # Migrate legacy raw .jsonl on remote -> .jsonl.gz, preserving mtime.
    for legacy in list(remote_sessions.glob("*.jsonl")):
        gz_path = remote_sessions / (legacy.name + ".gz")
        if gz_path.exists():
            # Both exist: keep the newer-mtime one, drop the loser.
            if gz_path.stat().st_mtime + 1 >= legacy.stat().st_mtime:
                log.append(f"  [sessions] migrate: drop redundant raw {legacy.name}")
                if not dry_run:
                    legacy.unlink()
            else:
                log.append(f"  [sessions] migrate: re-gzip {legacy.name} (newer)")
                if not dry_run:
                    gz_compress(legacy, gz_path)
                    legacy.unlink()
        else:
            log.append(f"  [sessions] migrate: gzip {legacy.name}")
            if not dry_run:
                gz_compress(legacy, gz_path)
                legacy.unlink()

    names = set()
    for p in local_root.glob("*.jsonl"):
        names.add(p.name)
    for p in remote_sessions.glob("*.jsonl.gz"):
        names.add(p.name[:-3])  # strip .gz, keep .jsonl

    for name in sorted(names):
        pl = local_root / name
        pr_gz = remote_sessions / (name + ".gz")
        if pl.exists() and pr_gz.exists():
            mtl, mtr = pl.stat().st_mtime, pr_gz.stat().st_mtime
            if mtl > mtr + 1:
                log.append(f"  [sessions] push {name}")
                if not dry_run:
                    bpath = backups_root / "sessions" / "remote" / (name + ".gz")
                    bpath.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(pr_gz, bpath)
                    log.append(f"    backup: sessions/remote/{name}.gz")
                    gz_compress(pl, pr_gz)
            elif mtr > mtl + 1:
                log.append(f"  [sessions] pull {name}")
                if not dry_run:
                    bpath = backups_root / "sessions" / "local" / name
                    bpath.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(pl, bpath)
                    log.append(f"    backup: sessions/local/{name}")
                    gz_decompress(pr_gz, pl)
        elif pl.exists():
            log.append(f"  [sessions] push {name} (new)")
            if not dry_run:
                gz_compress(pl, pr_gz)
        else:
            log.append(f"  [sessions] pull {name} (new)")
            if not dry_run:
                gz_decompress(pr_gz, pl)


def is_excluded(dropbox_root: Path, canonical: str) -> bool:
    """Return True if this canonical name is in the registry's excluded list."""
    reg_path = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
    if not reg_path.exists():
        return False
    try:
        data = json.loads(reg_path.read_text())
    except Exception:
        return False
    return canonical in data.get("excluded", [])


def set_excluded(dropbox_root: Path, canonical: str, exclude: bool):
    """Add or remove canonical from the registry's excluded list."""
    reg_path = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
    data = json.loads(reg_path.read_text()) if reg_path.exists() else {}
    excluded = data.setdefault("excluded", [])
    if exclude and canonical not in excluded:
        excluded.append(canonical)
        # Also remove from projects so --all doesn't visit it
        data.get("projects", {}).pop(canonical, None)
    elif not exclude and canonical in excluded:
        excluded.remove(canonical)
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(data, indent=2))
    action = "excluded" if exclude else "un-excluded"
    print(f"{canonical} {action}.")


def sync_project(abs_path: Path, dropbox_root: Path, backups_root: Path,
                 dry_run: bool, log: list):
    local_dir = resolve_project_dir(abs_path)
    canonical = canonical_name(abs_path)
    remote_dir = dropbox_root / SYNC_SUBPATH / PROJECTS_SUBDIR / canonical

    log.append(f"\n=== project: {canonical} ===")
    log.append(f"  local : {local_dir}")
    log.append(f"  remote: {remote_dir}")

    if is_excluded(dropbox_root, canonical):
        log.append("  SKIPPED (excluded — run with --unexclude to re-enable)")
        return

    if not check_collision(dropbox_root, canonical, abs_path, log):
        log.append("  SKIPPED due to collision")
        return

    sync_pair(local_dir / "memory", remote_dir / "memory",
              f"memory/{canonical}", backups_root, dry_run, log)
    sync_jsonl(local_dir, remote_dir / "sessions", backups_root, dry_run, log)
    if not dry_run:
        update_registry(dropbox_root, canonical, abs_path, local_dir)


def sync_skills(dropbox_root: Path, backups_root: Path, dry_run: bool, log: list):
    """Two-way sync ~/.claude/skills/ with Dropbox/.../skills/."""
    local = Path.home() / ".claude" / "skills"
    remote = dropbox_root / SYNC_SUBPATH / SKILLS_SUBDIR
    log.append(f"\n=== skills (global) ===")
    log.append(f"  local : {local}")
    log.append(f"  remote: {remote}")
    sync_pair(local, remote, "skills", backups_root, dry_run, log)


def check_collision(dropbox_root: Path, canonical: str, abs_path: Path, log: list) -> bool:
    """Warn if another host has registered the same canonical name at a different
    abs_path that looks unrelated. Returns True (proceed) or False (skip)."""
    reg_path = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
    if not reg_path.exists():
        return True
    try:
        data = json.loads(reg_path.read_text())
    except Exception:
        return True
    entry = data.get("projects", {}).get(canonical)
    if not entry:
        return True
    host = socket.gethostname()
    other_paths = {
        m: info.get("abs_path")
        for m, info in entry.get("machines", {}).items()
        if m != host and info.get("abs_path")
    }
    if not other_paths:
        return True
    my_name = abs_path.name.lower()
    for other_host, other_path in other_paths.items():
        # Use PureWindowsPath for Windows-style paths (drive letter + backslash)
        # so that basename extraction works correctly when running on Linux.
        import re as _re
        if _re.match(r'^[A-Za-z]:[/\\]', other_path):
            from pathlib import PureWindowsPath
            other_name = PureWindowsPath(other_path).name.lower()
        else:
            other_name = Path(other_path).name.lower()
        if other_name != my_name:
            log.append(
                f"  COLLISION: canonical='{canonical}' but machine '{other_host}' "
                f"registered it with folder name '{Path(other_path).name}'. "
                f"Set CLAUDE_SYNC_DROPBOX or rename one project to avoid merging "
                f"unrelated work. Skipping."
            )
            return False
    return True


def update_registry(dropbox_root: Path, canonical: str, abs_path: Path, local_dir: Path):
    reg_path = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
    if reg_path.exists():
        try:
            data = json.loads(reg_path.read_text())
        except Exception:
            data = {}
    else:
        data = {}
    projects = data.setdefault("projects", {})
    entry = projects.setdefault(canonical, {"machines": {}})
    host = socket.gethostname()
    entry["machines"][host] = {
        "abs_path": str(abs_path),
        "project_key": local_dir.name,
        "last_sync": datetime.now().astimezone().isoformat(),
    }
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    reg_path.write_text(json.dumps(data, indent=2))


def prune_old_sessions(dropbox_root: Path, dry_run: bool, log: list):
    """Drop *.jsonl.gz on Dropbox older than SESSION_RETENTION_DAYS.

    Local raw .jsonl files are the canonical archive and are never touched
    here — older sessions remain resumable on whichever machine recorded them.
    """
    projects_root = dropbox_root / SYNC_SUBPATH / PROJECTS_SUBDIR
    if not projects_root.exists():
        return
    cutoff = time.time() - SESSION_RETENTION_DAYS * 86400
    dropped = 0
    bytes_freed = 0
    for gz in projects_root.glob("*/sessions/*.jsonl.gz"):
        if gz.stat().st_mtime < cutoff:
            bytes_freed += gz.stat().st_size
            dropped += 1
            log.append(f"  [prune] drop {gz.relative_to(projects_root)} "
                       f"(>{SESSION_RETENTION_DAYS}d old)")
            if not dry_run:
                gz.unlink()
    if dropped:
        log.append(f"  [prune] sessions: {dropped} files, "
                   f"{bytes_freed/1e6:.1f} MB freed")


def prune_old_backups(dropbox_root: Path, dry_run: bool, log: list):
    """Trim backups/<timestamp>/ directories.

    Keep every backup whose mtime is within BACKUP_RETENTION_DAYS, and
    additionally keep at least BACKUP_RETENTION_MIN_RUNS most-recent ones.
    Anything older than both policies is removed.
    """
    backups_dir = dropbox_root / SYNC_SUBPATH / BACKUPS_SUBDIR
    if not backups_dir.exists():
        return
    runs = sorted(
        (d for d in backups_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
    )
    if not runs:
        return
    cutoff = time.time() - BACKUP_RETENTION_DAYS * 86400
    keep_recent = set(runs[-BACKUP_RETENTION_MIN_RUNS:])
    dropped = 0
    bytes_freed = 0
    for d in runs:
        if d in keep_recent:
            continue
        if d.stat().st_mtime >= cutoff:
            continue
        size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
        bytes_freed += size
        dropped += 1
        log.append(f"  [prune] drop backup {d.name}")
        if not dry_run:
            shutil.rmtree(d)
    if dropped:
        log.append(f"  [prune] backups: {dropped} runs, "
                   f"{bytes_freed/1e6:.1f} MB freed")


def export_bundle(out_path: Path, dropbox_root: Path, log: list):
    """Build a tar.gz of every local Claude project + skills, keyed by canonical
    name, for bootstrapping a fresh machine with full history.

    Bundle layout:
      manifest.json           - exported_at, host, projects, unregistered
      skills/                 - copy of ~/.claude/skills/
      projects/<canonical>/   - registered projects (have a canonical name)
        memory/...
        sessions/*.jsonl      - raw transcripts (outer tar.gz handles compression)
      unregistered/<key>/     - projects with content but no registry entry on
        memory/...              this host; receiving machine decides what to do
        sessions/*.jsonl        with them

    To restore on a new machine: extract the bundle, set up each project locally,
    run `python sync.py` once to register the local key, then copy
    bundle/projects/<canonical>/{memory,sessions} into the corresponding
    ~/.claude/projects/<key>/ directory.
    """
    claude_projects = Path.home() / ".claude" / "projects"
    claude_skills = Path.home() / ".claude" / "skills"

    reg_path = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
    registry = {}
    if reg_path.exists():
        try:
            registry = json.loads(reg_path.read_text())
        except Exception:
            registry = {}
    host = socket.gethostname()

    # Map this host's local project_key -> canonical name via the registry.
    key_to_canonical = {}
    for canonical, entry in registry.get("projects", {}).items():
        m = entry.get("machines", {}).get(host)
        if m and m.get("project_key"):
            key_to_canonical[m["project_key"]] = canonical

    manifest = {
        "exported_at": datetime.now().astimezone().isoformat(),
        "host": host,
        "schema_version": 1,
        "projects": {},
        "unregistered": {},
    }

    out_path = out_path.expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log.append(f"\n=== export-bundle ===")
    log.append(f"  out: {out_path}")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "bundle"
        staging.mkdir()

        # Skills.
        if claude_skills.exists():
            shutil.copytree(claude_skills, staging / "skills")
            log.append(f"  + skills/ ({claude_skills})")

        # Projects. Include every directory that has content; registered ones
        # land under projects/<canonical>, unregistered under unregistered/<local_key>
        # so the receiving machine can decide what to do with them.
        if claude_projects.exists():
            for proj_dir in sorted(claude_projects.iterdir()):
                if not proj_dir.is_dir():
                    continue
                memory_src = proj_dir / "memory"
                sessions = list(proj_dir.glob("*.jsonl"))
                if not memory_src.exists() and not sessions:
                    continue
                canonical = key_to_canonical.get(proj_dir.name)
                if canonical:
                    dst = staging / "projects" / canonical
                    bucket, label = "projects", canonical
                else:
                    dst = staging / "unregistered" / proj_dir.name
                    bucket, label = "unregistered", proj_dir.name
                dst.mkdir(parents=True, exist_ok=True)
                if memory_src.exists():
                    shutil.copytree(memory_src, dst / "memory")
                if sessions:
                    (dst / "sessions").mkdir(exist_ok=True)
                    for s in sessions:
                        shutil.copy2(s, dst / "sessions" / s.name)
                entry = {
                    "local_key": proj_dir.name,
                    "session_count": len(sessions),
                    "has_memory": memory_src.exists(),
                }
                if canonical:
                    entry["abs_path"] = (registry.get("projects", {})
                        .get(canonical, {}).get("machines", {})
                        .get(host, {}).get("abs_path"))
                    manifest["projects"][canonical] = entry
                else:
                    manifest["unregistered"][proj_dir.name] = entry
                log.append(f"  + {bucket}/{label} "
                           f"({len(sessions)} sessions, memory={memory_src.exists()})")

        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2))

        with tarfile.open(out_path, "w:gz", compresslevel=6) as tar:
            tar.add(staging, arcname="bundle")

    size_mb = out_path.stat().st_size / 1e6
    log.append(f"  wrote {size_mb:.1f} MB; "
               f"{len(manifest['projects'])} registered, "
               f"{len(manifest['unregistered'])} unregistered projects")


def write_log(dropbox_root: Path, log: list):
    log_dir = dropbox_root / SYNC_SUBPATH / LOG_DIR_NAME
    log_dir.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (log_dir / f"{host}-{stamp}.log").write_text("\n".join(log))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="Absolute project path (default: cwd)")
    ap.add_argument("--all", action="store_true", help="Sync every project in registry")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="Show registry then exit")
    ap.add_argument("--no-skills", action="store_true", help="Skip ~/.claude/skills/")
    ap.add_argument("--skills-only", action="store_true", help="Only sync skills")
    ap.add_argument("--exclude", metavar="NAME",
                    help="Add canonical project name to excluded list and stop syncing it")
    ap.add_argument("--unexclude", metavar="NAME",
                    help="Remove canonical project name from excluded list")
    ap.add_argument("--no-prune", action="store_true",
                    help="Skip the end-of-sync prune of old sessions and old backups")
    ap.add_argument("--export-bundle", metavar="PATH",
                    help="Write a tar.gz of all local memory + sessions + skills "
                         "to PATH for bootstrapping a fresh machine, then exit")
    args = ap.parse_args()

    dropbox_root = find_dropbox_root()
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backups_root = backup_dir(dropbox_root, run_stamp)

    log = [f"claude-memory-sync @ {datetime.now().isoformat()} on {socket.gethostname()}"]
    log.append(f"dropbox_root = {dropbox_root}")

    if args.list:
        reg = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
        if reg.exists():
            print(reg.read_text())
        else:
            print("(no registry yet)")
        return

    if args.exclude:
        set_excluded(dropbox_root, args.exclude, True)
        return

    if args.unexclude:
        set_excluded(dropbox_root, args.unexclude, False)
        return

    if args.export_bundle:
        export_bundle(Path(args.export_bundle), dropbox_root, log)
        print("\n".join(log))
        return

    targets: list[Path] = []
    if args.all:
        reg = dropbox_root / SYNC_SUBPATH / REGISTRY_NAME
        if not reg.exists():
            print("No registry — run without --all inside each project once first.")
            return
        data = json.loads(reg.read_text())
        host = socket.gethostname()
        for name, entry in data.get("projects", {}).items():
            m = entry.get("machines", {}).get(host)
            if m and m.get("abs_path"):
                p = Path(m["abs_path"])
                if p.exists():
                    targets.append(p)
                else:
                    log.append(f"  skip {name}: {p} not present on this machine")
    elif not args.skills_only:
        p = Path(args.project) if args.project else Path.cwd()
        targets.append(p.resolve())

    for t in targets:
        sync_project(t, dropbox_root, backups_root, args.dry_run, log)

    if not args.no_skills:
        sync_skills(dropbox_root, backups_root, args.dry_run, log)

    if not args.no_prune:
        log.append(f"\n=== prune ===")
        prune_old_sessions(dropbox_root, args.dry_run, log)
        prune_old_backups(dropbox_root, args.dry_run, log)

    if not args.dry_run:
        write_log(dropbox_root, log)
    print("\n".join(log))
    if args.dry_run:
        print("\n(dry run — no files were copied)")


if __name__ == "__main__":
    main()
