# rsnapshot_gastown

Manages SynoCommunity rsnapshot on **gastown** (Synology DSM 7.x). Codifies the
2026-05-12 immediate-fix from `~/silverbullet/AI Generated/Barbossa/Backup
Strategy Review.md`.

## What it does

1. Renders `rsnapshot.conf` to `/var/services/homes/nux/rsnapshot.conf` —
   on `/dev/md0` (the DSM system root), outside `SYNOPKG_PKGDEST/var` so the
   next package upgrade can't `rm` it.
2. Installs `~/rsnapshot-run.sh` (nux's home), a wrapper that emits explicit
   start / ok / fail ntfy messages (single topic `changeme_set_via_vault`,
   per-message priorities). Replaces the bug where the old chain
   (`ntfy && rsnapshot || ntfy`) fired the success notification on failures.
3. Rewrites the `cmd:` line in synoschedtask 4/5/6/7 to call the wrapper
   with the right interval. Backs up the original `.task` file once.
4. `validate:` on the template runs `rsnapshot configtest` before any
   change is committed to disk.

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

## Assumptions

- `nux` on gastown is in the DSM `administrators` group, which gives direct
  write access to every path this role touches. There is **no sudo for `nux`
  on gastown** (`sudo -n whoami` → "a password is required") — host_vars sets
  `ansible_become: false` and the role runs without `become` for everything
  except the one task that mkdirs under the root-owned `/volume1/NetBackup/`
  top level.
- `/usr/local/bin/rsnapshot` symlink is intact (it is, post-1.5.1-5).
- `ntfy` shell wrapper at `/usr/local/bin/ntfy` accepts `-p N` and
  `--tags=...` and posts to topic `changeme_set_via_vault`.

## Not handled here

- The borg/borgmatic citadel→gastown push tier (separate role).
- Off-site / firebox-USB tier (Phase 2 in the strategy doc).
- Grafana heartbeat panel (separate workstream).
