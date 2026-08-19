# rsnapshot

Manages SynoCommunity rsnapshot on gastown (Synology DSM 7.x). Codifies the
2026-05-12 immediate-fix from `~/silverbullet/AI Generated/Barbossa/Backup
Strategy Review.md`.

## What it does

1. Renders `rsnapshot.conf` to `/var/services/homes/nux/rsnapshot.conf`, which
   sits on `/dev/md0` (the DSM system root) outside `SYNOPKG_PKGDEST/var` so
   the next package upgrade can't `rm` it.
2. Installs `~/rsnapshot-run.sh` (nux's home), a wrapper that emits explicit
   start / ok / fail ntfy messages (single topic `changeme_set_via_vault`,
   per-message priorities). This replaces the bug where the old chain
   (`ntfy && rsnapshot || ntfy`) fired the success notification on failures.
3. Rewrites the `cmd:` line in synoschedtask 4/5/6/7 to call the wrapper with
   the right interval. Backs up the original `.task` file once.
4. Runs `rsnapshot configtest` via the template's `validate:` before any change
   is committed to disk.

## Inputs (variables you'd commonly override)

See `defaults/main.yml`. The list-shaped one:

```yaml
rsnapshot_sources:
  - { name: warrig,        ssh_target: nux@warrig.shadeking.cam.local,        source_path: /home/nux/ }
  - { name: citadel,       ssh_target: nux@citadel.shadeking.cam.local,       source_path: /home/nux/ }
  - { name: wellerman,     ssh_target: pirate@wellerman.shadeking.cam.local,  source_path: /home/pirate/ }
  - { name: black-pearl,   ssh_target: pirate@black-pearl.shadeking.cam.local, source_path: /home/pirate/ }
  - { name: billy-of-tea,  ssh_target: pirate@billy-of-tea.shadeking.cam.local, source_path: /home/pirate/ }
```

## Invocation

```sh
# dry-run against gastown:
ansible-playbook --check --diff playbooks/configure_rsnapshot.yml

# apply:
ansible-playbook playbooks/configure_rsnapshot.yml
```

## Overlapping intervals, and why the wrapper takes its own lock

rsnapshot uses a single lockfile for all four intervals. On contention it does
not queue: `add_lockfile()` prints "Lockfile ... exists and so does its
process, can not continue" and exits **1** before touching any source. Exit 1
is rsnapshot's generic fatal code, indistinguishable from a bad config or an
unwritable snapshot root, so the wrapper's `else` branch paged it as FAILED.
That is the same class of false alarm the April 13 rewrite was written to
eliminate, arriving by a different route.

Measured 2026-08-18, and the collision is structural rather than occasional:

```
00:00:02  hourly started
00:20:04  daily started        <- 20 min into a 34 min hourly
00:20:06  daily FAILED         <- "exited 1", dead in two seconds
00:34:28  hourly completed with warnings
```

Across twelve consecutive hourly runs the duration ranged from 23 to 50
minutes. Daily fires at a fixed `:20`, so even the fastest hourly outlasts
daily's offset. Daily was losing the race every single day.

The fix is a wrapper-level `flock` on `rsnapshot_wrapper_lockfile`, held for up
to `rsnapshot_lock_wait_seconds` (default 3600).

**It waits rather than skips, and that difference is the point.** The sibling
`borgmatic-run.sh` uses `flock -n` and exits immediately, which is correct
there: borgmatic has one daily schedule, so a collision only happens against a
hand-run and the skipped copy is redundant. Here the intervals are retention
tiers. A skipped `daily` means `daily.0` never rotates and that day's tier is
gone, silently. Waiting costs a delayed snapshot; skipping costs a missing one.
Because `flock` queues, daily now simply begins when hourly finishes, and the
next hourly queues behind it.

If the wait expires, the run reports `rsnapshot <interval> skipped` at the warn
priority: visible in history, not a page, because no backup actually broke. But
a tier genuinely did not run, so repeated skips mean hourly has outgrown the
schedule and the DSM times need spreading out. That part is a Cam action, since
synoschedtask is root-owned.

Verified on gastown before deploying: `flock` is util-linux 2.33.2, a waiter
blocks and then acquires when the holder releases, and the `-w` timeout path
returns rather than hanging.

## Assumptions

- `nux` on gastown is in the DSM `administrators` group, which gives direct
  write access to every path this role touches. There is no sudo for `nux` on
  gastown (`sudo -n whoami` returns "a password is required"), so host_vars
  sets `ansible_become: false` and the role runs without `become` for
  everything except the one task that mkdirs under the root-owned
  `/volume1/NetBackup/` top level.
- The `/usr/local/bin/rsnapshot` symlink is intact (it is, post-1.5.1-5).
- The `ntfy` binary at `rsnapshot_ntfy_bin` accepts `-p`, `--tags=...` and a
  `host/topic` argument. The real topic comes from
  `inventory/group_vars/synology/vault.yml`; the `changeme_set_via_vault`
  default is a sentinel so an unconfigured run fails loudly. Keep the vault
  value **host-qualified** (`ntfy.coder.cam/<topic>`): the wrapper runs as root
  under synoschedtask, and with a bare topic the only thing keeping alerts off
  the public ntfy.sh is root's own unmanaged ntfy config. See
  `defaults/main.yml`.

## Not handled here

- The borg/borgmatic citadel to gastown push tier (separate role).
- Off-site and firebox-USB tier (Phase 2 in the strategy doc).
- Grafana heartbeat panel (separate workstream).

## Upstream

Automates [rsnapshot](https://rsnapshot.org/) (GPL-2.0), as packaged for DSM by
[SynoCommunity](https://github.com/SynoCommunity/spksrc). Scheduling goes
through Synology's proprietary `synoschedtask`, and notifications through
[ntfy](https://github.com/binwiederhier/ntfy) (Apache-2.0 or GPL-2.0). This
role renders configuration and a wrapper script; it ships no upstream code.
