# dsm_config_audit

Read-only DSM config-drift snapshot for gastown. Drops an export script in
`nux`'s home and pins the Windmill runner's SSH key to it with a forced
`command=`, so the Windmill `dsm-config-audit` flow can SSH in nightly, capture
the export, commit one file per surface to a gitea repo, and alert on drift.

Phase 1 of [[AI Generated/Barbossa/Homelab/DSM Config Backup Plan]] (approach
C, the pull model). Closes the detection gap behind the April rogue
`synoschedtask id=4` that ran 700+ times over a month unnoticed.

## Why it runs unprivileged

On gastown `nux` has no sudo (`ansible_become: false`,
`inventory/host_vars/gastown.yml`). The role therefore uses no `become`, every
path it writes is under nux's own home, and the export script runs as plain
`nux`.

`nux` is in the DSM `administrators` group, which is enough to read the
highest-value surface directly: the raw `*.task` files under
`/usr/syno/etc/synoschedule.d/root/`, exactly where id=4 lived. Surfaces that
truly need uid 0 (`synopkg`/`synofirewall` authoritative output, `/etc/shadow`,
root's crontab, `/etc/sudoers`) degrade to a stable `# [WARN]` marker in the
output instead of vanishing, so a missing surface shows up as a constant diff
line rather than silent absence.

> Decision for Cam: if you want the root-only surfaces (authoritative
> `synopkg`, `synofirewall`, `/etc/sudoers`, root crontab) actually captured,
> the runner needs more than nux-unprivileged. Either a narrow `sudoers` grant
> pinned to this script, or run the key under a privileged account. Until then
> those surfaces show as `# [WARN]`. The scheduled-task, package (via
> `/var/packages`), user, SSH, synoinfo, and network surfaces are fully
> captured as nux.

## What it captures (stdout, one section per surface)

Scheduled tasks (`.task` files plus `synoschedtask --list`), SMB shares and
ACLs, users/groups/sudoers (passwd and group; shadow is excluded, being
root-only and a hash-leak risk with no extra signal), SSH (`sshd_config` plus
public-key fingerprints only, never private keys), certificates (chain and
metadata via `openssl`, private keys excluded), installed packages, crontabs,
`synoinfo.conf` delta vs defaults, firewall rules, and network config.

### Output contract (Phase 2 depends on this)

Each surface starts with a line of the exact form:

```
===== FILE: <name> =====
```

Everything up to the next delimiter (or EOF) is the body Windmill writes to
`<name>` in the repo. The script emits no timestamps or run-varying data. The
git commit carries the time, and stray timestamps would churn the diff (DSM
already auto-rotates some of its own; see the plan premortem).

## Security

Never exports secrets: no SSH private keys, no certificate private keys, no
`/etc/shadow` hashes. The Phase-1 hand-review task (`assignee: cam`) exists so
someone eyeballs the first capture before it is scheduled. Do that before
pointing Windmill at a writable gitea repo.

## Variables

See `defaults/main.yml`. The one you must set is `dsm_audit_windmill_pubkey`,
the Windmill runner's public key. Put the real value in
`inventory/group_vars/synology/vault.yml` as `vault_dsm_audit_windmill_pubkey`
and reference it from `vars.yml`, the same pattern as `rsnapshot_ntfy_topic`.
The role asserts the sentinel default is overridden before installing the key.
Set `dsm_audit_install_authorized_key: false` to template the script alone for
a first hand-review without granting any key access.

## Usage

```sh
ansible-playbook playbooks/configure_dsm_config_audit.yml
```

Wired into `playbooks/configure_dsm_config_audit.yml`, hosts: `synology`.
Live on gastown, with the export script deployed and the forced-command
Windmill key in place. Pairs with the Windmill `dsm-config-audit` flow
(Phase 2). Confirm the DSM 7.3 `synofirewall` subcommand before relying on
the firewall surface.

## Upstream

Wraps Synology DSM's own command-line utilities (`synoschedtask`, `synopkg`,
`synofirewall`, `synouser`), which are proprietary and vendor-supplied. No DSM
code is copied or redistributed here; the export script only invokes them and
reads their output. Certificate inspection uses
[OpenSSL](https://github.com/openssl/openssl) (Apache-2.0). The consuming
nightly flow runs on [Windmill](https://github.com/windmill-labs/windmill).
