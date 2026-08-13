# borgmatic_warrig

Minimal companion role to **warrig's hand-managed borgmatic**. It installs the
Arch system-state export script and the `~/.system-state/` directory it writes
into, so warrig captures the same machine-shape recovery state that
`borgmatic_citadel` captures (Phase 1 of the *Borgmatic Package-list Export*
plan). Phase 2 = this role.

## What it does

1. Creates `/home/nux/.system-state/` (`nux:nux`, `0750`) — rides in warrig's
   existing `/home/nux` backup source, so its contents land in every archive.
2. Installs `/usr/local/bin/borgmatic-export-system-state.sh` (`root:root`,
   `0750`) — byte-identical to citadel's. Captures explicit/foreign/orphan
   pacman lists, full `pacman -Qi` metadata, `pacman.conf`, `mirrorlist`,
   `fstab`, enabled systemd units, `timedatectl`, and hostname.
3. Removes the legacy `borgmatic-export-pkglists.sh` if a previous attempt left
   one behind.

## What it deliberately does NOT do

This role does **not** render `/etc/borgmatic/config.yaml`. Warrig's borgmatic
config is still hand-managed there, and codifying the whole thing is explicitly
out of scope for this pass (see the plan's "Out of scope" item #2 and premortem).
Because of that, the `before_backup` hook is **not** auto-wired — blindly
mutating a hand-managed, backup-load-bearing YAML file we haven't captured is
the kind of change that can silently break backups. So the wiring is a
documented one-time manual edit below.

## Wiring the hook (one-time manual edit on warrig)

After applying this role, edit `/etc/borgmatic/config.yaml` on warrig and add
two things:

```yaml
source_directories:
    # ...existing entries...
    - /home/nux/.system-state        # capture machine-shape state

# borgmatic >= 1.8 style:
before_backup:
    # ...existing entries...
    - /usr/local/bin/borgmatic-export-system-state.sh
```

(If warrig's borgmatic uses the newer `commands:` block instead of top-level
`before_backup:`, add the script as a `before: action` entry there instead —
match whatever shape the existing config already uses.)

Then verify the hook fires and the files land, without touching the real repo:

```sh
sudo /usr/local/bin/borgmatic-export-system-state.sh   # run the script once
ls -la /home/nux/.system-state/                        # confirm files appear
sudo borgmatic --config /etc/borgmatic/config.yaml create --dry-run -v 1
```

## Apply

```sh
ansible-playbook -i inventory/backup_targets playbooks/configure_borgmatic_warrig.yml
```

(warrig lives in the `arch_backup_targets` group of `inventory/backup_targets`.)

## Once warrig's config is eventually codified

If/when Cam decides to promote warrig's `/etc/borgmatic/config.yaml` into a full
Ansible-owned template (the open question on the plan page), fold the
`before_backup` entry and the `source_directories` line into that template and
drop the manual step above. At that point this role can either merge into the
config role or stay as the script-only piece it depends on.
