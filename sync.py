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

Usage:
  python sync.py                   # sync project in cwd + skills
  python sync.py --project /abs    # sync a different project
  python sync.py --all             # sync every registered project on this machine
  python sync.py --skills-only     # skip project work
  python sync.py --no-skills       # skip skills
  python sync.py --dry-run
  python sync.py --list            # print registry
"""
from __future__ import annotations
import argparse, json, os, shutil, socket, sys, filecmp
from pathlib import Path
from datetime import datetime

SYNC_SUBPATH = "claude-memory-sync"
PROJECTS_SUBDIR = "projects"
SKILLS_SUBDIR = "skills"
BACKUPS_SUBDIR = "backups"
REGISTRY_NAME = "registry.json"
LOG_DIR_NAME = "logs"


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


def sync_jsonl(local_root: Path, remote_sessions: Path, backups_root: Path,
               dry_run: bool, log: list):
    """Sync *.jsonl at the project root only (not recursive)."""
    remote_sessions.mkdir(parents=True, exist_ok=True)
    local_root.mkdir(parents=True, exist_ok=True)
    names = set()
    for p in local_root.glob("*.jsonl"):
        names.add(p.name)
    for p in remote_sessions.glob("*.jsonl"):
        names.add(p.name)
    for name in sorted(names):
        pl = local_root / name
        pr = remote_sessions / name
        if pl.exists() and pr.exists():
            mtl, mtr = pl.stat().st_mtime, pr.stat().st_mtime
            if mtl > mtr + 1:
                log.append(f"  [sessions] push {name}")
                copy_with_backup(pl, pr, backups_root,
                                 Path("sessions") / "remote" / name, dry_run, log)
            elif mtr > mtl + 1:
                log.append(f"  [sessions] pull {name}")
                copy_with_backup(pr, pl, backups_root,
                                 Path("sessions") / "local" / name, dry_run, log)
        elif pl.exists():
            log.append(f"  [sessions] push {name} (new)")
            if not dry_run:
                shutil.copy2(pl, pr)
        else:
            log.append(f"  [sessions] pull {name} (new)")
            if not dry_run:
                shutil.copy2(pr, pl)


def sync_project(abs_path: Path, dropbox_root: Path, backups_root: Path,
                 dry_run: bool, log: list):
    local_dir = resolve_project_dir(abs_path)
    canonical = canonical_name(abs_path)
    remote_dir = dropbox_root / SYNC_SUBPATH / PROJECTS_SUBDIR / canonical

    log.append(f"\n=== project: {canonical} ===")
    log.append(f"  local : {local_dir}")
    log.append(f"  remote: {remote_dir}")

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

    if not args.dry_run:
        write_log(dropbox_root, log)
    print("\n".join(log))
    if args.dry_run:
        print("\n(dry run — no files were copied)")


if __name__ == "__main__":
    main()
