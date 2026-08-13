# dsm_config_audit

Read-only **DSM config-drift snapshot** for gastown. Drops an export script in
`nux`'s home and pins the Windmill runner's SSH key to it with a forced
`command=`, so the Windmill `dsm-config-audit` flow can SSH in nightly, capture
the export, commit one file per surface to a gitea repo, and alert on drift.

Phase 1 of [[AI Generated/Barbossa/Homelab/DSM Config Backup Plan]] (approach
**C**, the pull model). Closes the detection gap behind the April rogue
`synoschedtask id=4` that ran 700+ times over a month unnoticed.

## Why it runs unprivileged

On gastown `nux` has **no sudo** (`ansible_become: false`,
`inventory/host_vars/gastown.yml`). So the role uses no `become` — every path it
writes is under nux's own home — and the export script runs as plain `nux`.

`nux` is in the DSM `administrators` group, which is enough to read the
highest-value surface directly: the raw `*.task` files under
`/usr/syno/etc/synoschedule.d/root/` — exactly where id=4 lived. Surfaces that
truly need uid 0 (`synopkg`/`synofirewall` authoritative output, `/etc/shadow`,
root's crontab, `/etc/sudoers`) degrade to a **stable `# [WARN]` marker** in the
output instead of vanishing, so a missing surface is a constant diff line rather
than silent absence.

> **Decision for Cam:** if you want the root-only surfaces (authoritative
> `synopkg`, `synofirewall`, `/etc/sudoers`, root crontab) actually captured,
> the runner needs more than nux-unprivileged — either a narrow `sudoers` grant
> pinned to this script, or run the key under a privileged account. Until then
> those surfaces show as `# [WARN]`. The scheduled-task, package (via
> `/var/packages`), user, SSH, synoinfo, and network surfaces are fully captured
> as nux.

## What it captures (stdout, one section per surface)

Scheduled tasks (`.task` files + `synoschedtask --list`), SMB shares + ACLs,
users/groups/sudoers (passwd/group; **shadow excluded** — root-only and a
hash-leak risk with no extra signal), SSH (`sshd_config` + public-key
**fingerprints only**, never private keys), certificates (chain + metadata via
`openssl`, **private keys excluded**), installed packages, crontabs,
`synoinfo.conf` delta vs defaults, firewall rules, and network config.

### Output contract (Phase 2 depends on this)

Each surface starts with a line of the exact form:

```
===== FILE: <name> =====
```

Everything up to the next delimiter (or EOF) is the body Windmill writes to
`<name>` in the repo. The script emits **no timestamps or run-varying data** —
the git commit carries the time; stray timestamps would churn the diff (DSM
already auto-rotates some of its own; see the plan premortem).

## Security

Never exports secrets: no SSH private keys, no certificate private keys, no
`/etc/shadow` hashes. The Phase-1 hand-review task (`assignee: cam`) exists to
eyeball the **first** capture before it is scheduled — do that before pointing
Windmill at a writable gitea repo.

## Variables

See `defaults/main.yml`. The one you must set: `dsm_audit_windmill_pubkey` — the
Windmill runner's **public** key. Put the real value in
`inventory/group_vars/synology/vault.yml` as `vault_dsm_audit_windmill_pubkey`
and reference it from `vars.yml` (same pattern as `rsnapshot_ntfy_topic`); the
role asserts the sentinel default is overridden before installing the key. Set
`dsm_audit_install_authorized_key: false` to template the script alone for a
first hand-review without granting any key access.

## Usage

```yaml
- hosts: gastown
  roles:
    - dsm_config_audit
```

Authoring only — not yet wired into a playbook or applied. Pairs with the
Windmill `dsm-config-audit` flow (Phase 2). Confirm the DSM 7.3 `synofirewall`
subcommand before relying on the firewall surface.
