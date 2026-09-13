# Scheduled tasks on the iblu box

Every recurring job that runs on `178.104.122.152` (the Hetzner box hosting
iblu-mcp) is listed here. This is the source of truth — if a cron exists on
the box but is not documented here, either add it or remove it.

Each task lists **where** it lives, **when** it fires, **what** it does,
**where** it logs, and **how to verify** it's still working. Ignas is a
non-developer; verification steps are intentionally one-liners.

---

## disk-cleanup — weekly

**Script:** `/home/ignas/mission-control/disk-cleanup/cleanup.sh`
**Cron file:** `/etc/cron.d/iblu-cleanup` (installed 2026-09-13)
**Schedule:** Sundays 04:00 local time
**Runs as:** root

**What it does:**
- Caps systemd journal to 500 MB / 14 days
- `apt-get clean` — clears downloaded .deb archives
- Purges pip wheel cache (both user pip and iblu venv pip)
- `npm cache clean --force`
- `docker builder prune -af` — clears all Docker build cache
- `docker image prune -f` — **dangling images only, deliberately NOT `-a`**

**Why not `-a` for image prune:** two locally-built images that no
registry can restore would be at risk — `posla-app:latest` (its source
directory no longer exists on this box) and `radovi-migrate:latest`
(migration image for a running production stack). Their reclaim is a
deliberate manual decision, not an automatic weekly one.

**Log:** `/var/log/iblu-cleanup.log` (append-only, every run adds a block)

**Health check (30-second manual verification):**
```bash
ssh ignas@178.104.122.152
# When did it last run?
sudo tail -3 /var/log/iblu-cleanup.log
# Is the cron file still there?
ls -l /etc/cron.d/iblu-cleanup
```

The log's last block should be from within the last 8 days and end with
`=== cleanup end ===`. If it doesn't, cron is failing silently — check
`sudo systemctl status cron` and `sudo journalctl -u cron -n 50`.

**Automatic health surface:** `get_infra_status` MCP tool includes a
`scheduled_tasks` block (see [infra-collector integration](#infra-collector-integration)
below) that flags this cron as `stale` if it hasn't run in >8 days.

---

## infra-collector — every 6 hours

**Script:** `/home/ignas/mission-control/infra-collector/collect.sh`
**Cron:** installed on this box (not `/etc/cron.d/`, personal crontab)
**Schedule:** every 6 hours at :05 (00:05, 06:05, 12:05, 18:05 UTC)
**Runs as:** ignas

**What it does:** SSHes into 3 spoke hosts (appsdev, appsprod, n8n),
runs `inventory.sh` on each, aggregates into `~/.infra/latest.json` +
`~/.infra/latest.md`, uploads both to a Drive folder.

**Log:** stderr goes to the crontab-configured mail spool. Reports
themselves are the authoritative "did it run" signal — check timestamps
on the Drive files or on `~/.infra/latest.md`.

**Health check (via IBLU):** in any Claude chat, ask "what's the
infrastructure status?" — this calls `get_infra_status` which reads the
Drive report. Status `stale` means the collector hasn't run in >24h.

**Health check (manual):**
```bash
ssh ignas@178.104.122.152
ls -l ~/.infra/latest.md    # mtime should be within last 6h
```

---

## iblu-mcp readstate subscription refresh — every 6 days

Not a cron — this runs inside the iblu-mcp systemd service as a
background thread. Documented here for completeness.

**Owner:** `src/iblu_keeper/readstate_worker.py :: _run_refresh_loop`
**Schedule:** on server start + every 6 days thereafter
**What it does:** ensures exactly one Google Workspace Events
subscription for `google.workspace.chat.spaceReadState.v1.updated` is
live, targeting `//cloudidentity.googleapis.com/users/{our-id}` with
`iblu-chat-events` Pub/Sub topic as notification endpoint. Google TTLs
these subscriptions at 7 days.

**Log:** `sudo journalctl -u iblu-mcp | grep readstate` — should show
either `readstate sub already live` (idempotent no-op) or
`readstate sub created` after every refresh.

**Health check:** `curl -s http://172.19.0.1:8000/health | jq .readstate_worker`
should show `ready: true` and `worker_alive: true`.

---

## Adding a new scheduled task

1. Put the script under `/home/ignas/mission-control/<name>/` (matching
   `disk-cleanup` / `infra-collector` pattern).
2. Install as `/etc/cron.d/iblu-<name>` (system cron, root, survives
   user account changes) or as a systemd timer if precise scheduling
   matters.
3. Have the script log to `/var/log/iblu-<name>.log` and always end its
   log block with a clear terminator so `tail -3` tells you if the last
   run finished cleanly.
4. Add a section to this file listing schedule + log location + a
   one-liner health check.
5. If it should be surfaced through IBLU, extend
   `infra-collector/inventory.sh` to add it to the `scheduled_tasks`
   block (see below).

---

## infra-collector integration

The collector's `inventory.sh` reads `/etc/cron.d/iblu-*` + tails their
log files and emits a `scheduled_tasks` block in `latest.json`:

```json
"scheduled_tasks": [
  {
    "name": "disk-cleanup",
    "schedule": "0 4 * * 0",
    "last_run": "2026-09-13T10:58:09Z",
    "hours_ago": 3,
    "status": "healthy"
  }
]
```

`status` is derived from `hours_ago` vs. expected interval:
- `healthy` — ran within 1.5× its expected interval
- `stale` — ran but overdue
- `never_ran` — cron file present but log missing/empty
- `missing` — expected but no cron file found

`get_infra_status` picks this up automatically (it just reads the
latest report), so any Claude chat can see cron health at a glance.
