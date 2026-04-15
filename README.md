# claude-memory-sync

I work between an Ubuntu laptop when I'm travelling and a Windows desktop at home, and I keep project files in sync through Dropbox. Claude Code's memory feature is great — it remembers who I am, how I like to work, and what's going on in each project, so I don't re-explain it every session — but the memories live on one machine at a time. Each time I switched laptops, the other Claude had stale context, or none at all. I kept telling it to ignore its own memory because the memory was wrong. I also lost the ability to `/resume` recent sessions on the other machine.

So I wrote this. It's a ~300-line Python script that two-way syncs Claude Code's memory files, session transcripts, and skills between machines via a folder in Dropbox.

## Why not tawanorg/claude-sync?

I found [tawanorg/claude-sync](https://github.com/tawanorg/claude-sync) first, and it's genuinely more serious than this: Go binary, Cloudflare R2 / S3 / GCS backend, end-to-end encryption with age, proper conflict detection. If you need any of that, use it.

The reason it didn't work for me: it identifies projects by absolute filesystem path. My project lives at `~/Dropbox/Projects/foo` on Linux and `D:\Dropbox\Projects\foo` on Windows, which Claude Code treats as two unrelated projects. tawanorg's recommended workaround is to keep identical paths on every machine or to use symlinks. I didn't want to do either.

I already pay for Dropbox, already trust it with my code, and wanted something I could read in one sitting. This tool identifies projects by folder *basename* and keeps a small registry mapping each machine's local path. `foo` on Linux and `foo` on Windows end up as one logical entry.

## What it does

- Two-way syncs `~/.claude/projects/<project>/memory/` (your memory files) and the project's `*.jsonl` session transcripts so `/resume` works across machines.
- Also syncs `~/.claude/skills/` globally, so a skill you install on one laptop shows up on the other.
- Uses the project folder's basename as identity, recording each machine's absolute path in `Dropbox/claude-memory-sync/registry.json`.
- Newest-mtime-wins per file. Never deletes.
- Before overwriting a file whose contents differ, copies the older version to `Dropbox/claude-memory-sync/backups/<timestamp>/` — so last-write-wins is at least recoverable.
- Refuses to sync if two machines try to register different projects under the same basename (e.g. two different `website/` directories), and tells you which one collided.

## Install

Requires Python 3.8+, Dropbox running on both machines, and the repo cloned somewhere you can run it from.

```bash
git clone https://github.com/siddharth-bharath/claude-memory-sync ~/.claude/skills/claude-memory-sync
```

Cloning into `~/.claude/skills/` also makes it show up as a Claude Code skill, so you can say "sync my memory" in a session and Claude will find it.

If your Dropbox root isn't `~/Dropbox` or a common alternative, export `CLAUDE_SYNC_DROPBOX=/path/to/Dropbox`.

## Use

From any project directory:

```bash
python ~/.claude/skills/claude-memory-sync/sync.py
```

Syncs that project's memory and sessions, plus global skills. Run it at the start and end of a work session, or whenever you switch machines.

Useful flags:

- `--all` — sync every project registered from this machine (run once per project first to register).
- `--dry-run` — show what would change, copy nothing.
- `--skills-only` / `--no-skills` — sync just skills, or skip them.
- `--project /abs/path` — sync a project other than the current directory.
- `--list` — print the registry.

## Honest caveats

Please read these before you rely on this tool.

**Last-write-wins conflict handling.** If you edit the same memory file on both machines between syncs, the older edit is overwritten. The `backups/` folder keeps a copy so you can recover, but there's no diff or merge. If you need proper multi-writer conflict handling, use tawanorg's tool.

**No encryption.** Your memory and session files sit in your Dropbox folder as plaintext. Session transcripts can contain API keys, code, and other secrets. You're trusting Dropbox's at-rest encryption and your account security. If that's not acceptable, use tawanorg's encrypted version.

**Dropbox is a hard dependency.** If the Dropbox client isn't running on both machines, nothing useful happens. This is a thin layer on top of Dropbox, not a replacement for it. Any other folder-syncing tool (iCloud Drive, OneDrive, Syncthing) should work if you point `CLAUDE_SYNC_DROPBOX` at it, but I only use Dropbox.

**Single-writer assumption.** Running Claude Code on the same project on both machines simultaneously, with syncs in between, will race.

**Basename collisions.** The tool now detects when two unrelated projects share a folder name and refuses to sync either, but you'll have to rename one to proceed.

## Credit

Inspired by [tawanorg/claude-sync](https://github.com/tawanorg/claude-sync), which is more feature-complete, encrypted, and cloud-backed. Start there if you need any of that. This is the simpler, Dropbox-only, works-even-when-your-paths-don't-match version.

## License

MIT.
