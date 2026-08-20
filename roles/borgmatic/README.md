# borgmatic

Nightly push of a borg archive to gastown, one repo per source host. Merged
2026-08-19 from `borgmatic_citadel` + `borgmatic_warrig`, which were ~85%
identical roles whose real differences were values, not logic.

Two hosts use it today:

| | repo on gastown | cron | sources | excludes | DB dumps |
|---|---|---|---|---|---|
| citadel | `borg_backup_citadel` | 03:00 | 5 | 15 | 2 (windmill pg, bunkerweb mariadb) |
| warrig | `borg_backup_warrig` | 04:00 | 3 | 26 | none, deliberately |

The hours are offset so gastown is never servicing two concurrent receivers.

## Where the values live

Everything host-specific is in `inventory/host_vars/<host>.yml`, not in this
role:

```yaml
borgmatic_repo_url            # the ssh:// repo on gastown
borgmatic_repo_label          # citadel / warrig, see "derived strings" below
borgmatic_passphrase_var      # names this host's ansible-vault key
borgmatic_cron_hour           # 3 / 4
borgmatic_source_directories
borgmatic_excludes
borgmatic_databases           # optional; [] means no DB hooks at all
```

`defaults/main.yml` carries the shared logic knobs and is documented inline.
Its defaults reproduce citadel's rendered output with no host_vars at all;
warrig overrides `borgmatic_ssh_command`, `borgmatic_checkpoint_interval` and
`borgmatic_run_flags` on top.

### Derived strings: do not "simplify" these

Two strings are derived from `borgmatic_repo_label` and must keep rendering
their existing literals byte-for-byte:

- `borgmatic_cron_name` = `borgmatic <label>→gastown daily`. The `cron:`
  module keys on this string. Change it and the live crontab line is orphaned
  while a *second* nightly run is added against the same repo.
- the blockinfile marker = `# {mark} ANSIBLE MANAGED: borgmatic_<label>`.
  A bare `borgmatic` marker would orphan the block already in
  `/root/.ssh/config` on both hosts and append a duplicate.

`borgmatic_repo_label` rather than `inventory_hostname` is also what the ntfy
titles, tags and message bodies derive from, so renaming a host in inventory
can never silently change the borg repo label.

## What it does

1. Installs `borgmatic` and `borg` (`state: present`, a no-op on both hosts).
2. Writes `/etc/borgmatic/passphrase` (root `0600`) from the ansible-vault key
   named by `borgmatic_passphrase_var`. The config's `encryption_passcommand`
   cats it, so the secret is in neither the config nor git.
   `-e borgmatic_manage_passphrase=false` skips both the assert and the write.
3. Generates `/root/.ssh/borg_gastown` (ed25519), installs that pubkey into
   `nux`'s `authorized_keys` on gastown (`delegate_to`, so it happens *before*
   anything dials across), renders a matching
   `Host gastown.shadeking.cam.local` block into `/root/.ssh/config` with
   `IdentitiesOnly yes`, and seeds known_hosts with one `BatchMode` probe.
   `-e borgmatic_manage_ssh_config=false` skips the ssh-config half.
4. Copies any pre-existing config aside once to
   `/etc/borgmatic/config.yaml.pre-ansible.bak` (`force: false`, so never
   overwritten on a later run).
5. Installs the scripts under `/usr/local/bin/`:
   - `borgmatic-export-system-state.sh`, a `before: action` hook capturing
     pacman state, pacman/mirror config, fstab, enabled units and host identity
     into `~/.system-state` so a restore can replay the host. Supersedes the
     older `borgmatic-export-pkglists.sh`, which the role removes.
   - `borgmatic-run.sh`, the cron entry-point, `flock`'d, with an explicit
     start/ok/fail ntfy chain so a partial pipe can't fire "ok" on a real
     failure.
   - `borgmatic-dump-dbs.sh` and `borgmatic-cleanup-dumps.sh`, only when
     `borgmatic_databases` is non-empty. See below.
6. Renders `/etc/borgmatic/config.yaml` (root `0600`), gated by
   `borgmatic config validate -c %s` so a malformed template never lands.
   Rendered last, after every script it references, so the validator runs
   against a config whose hook targets already exist on disk.
7. Adopts, never re-inits, an existing repo: `timeout <n> borgmatic repo-info`
   probes first and `/var/lib/borgmatic/.initialized` guards afterwards. rc 124
   (probe timed out) counts as "assume it exists".
8. Root cron: `borgmatic-run.sh` at `borgmatic_cron_hour`, logging to
   `/var/log/borgmatic.log`.

The role does NOT trigger a first manual `borgmatic create`. The first
scheduled fire is the validation: the start/ok/fail ntfy chain surfaces any
breakage immediately.

## Databases

Set `borgmatic_databases` in host_vars to a list of entries and the role grows
three things: the `/var/backups/borgmatic-dumps` staging dir, the two dump
scripts, and the `before:`/`after:` hooks in the config. Leave it `[]` (the
default) and none of that exists.

borgmatic's native `postgresql_databases` / `mariadb_databases` hooks would
need `pg_dumpall` / `mariadb-dump` on the host plus direct network access into
the containers. The DBs run inside docker without those binaries on the host,
so dumps are staged by a `before: action` hook via `docker exec`, then the
staging dir is captured as a regular source directory and the `after: action`
hook wipes it.

`borgmatic-dump-dbs.sh` is serialised with `flock` on the dump dir, runs under
`umask 077`, uses a split inner/outer `timeout`, and checks a terminator string
so a truncated-but-exit-0 dump is never promoted. It **always exits 0**: a DB
stumble degrades to an `urgent` ntfy plus a `DUMP-FAILED-<name>.txt` marker
rather than costing the filesystem backup.
`borgmatic-cleanup-dumps.sh` wipes the dump dir on both success and failure and
asserts each DB left either a dump or a marker, which catches a dump script
that never ran at all.

Entry shape:

```yaml
borgmatic_databases:
  - name: windmill-postgres          # also the DUMP-FAILED-<name>.txt suffix
    dump_file: "windmill-postgres-all.sql"   # cleanup asserts "<name>-all.sql"
    terminator: "PostgreSQL database cluster dump complete"
    dump_invocation: |-
      timeout "$outer_timeout" docker exec windmill-db-1 \
          timeout "$inner_timeout" pg_dumpall -U postgres
```

`dump_invocation` is rendered verbatim as the argv handed to the script's
`dump()` helper and references `$outer_timeout` / `$inner_timeout`, which come
from `borgmatic_dump_timeout_outer` (wraps `docker`, backstop for a wedged CLI)
and `borgmatic_dump_timeout_inner` (runs inside the container, actually bounds
the dump). **Outer must stay larger than inner**, else the CLI is killed while
the dump keeps running server-side and a retry stacks a second dump on the
first.

### warrig has none, deliberately

warrig runs a pile of containerised DBs (`immich_postgres`,
`atuin-postgresql-1`, `hoppscotch-postgres-1`, `plausible-plausible_db-1`,
`surrealdb`, `ansible-semaphore-postgres-1`, meilisearch, redis/valkey, …).
Nothing in this repo or its git history indicates warrig ever dumped them.
Decided 2026-08-18 and not merely deferred: warrig keeps the pre-Ansible
behaviour, where container data rides along inside `/home/nux` as ordinary
files copied mid-write. Be honest about what that buys: a file-level copy of a
running Postgres restores to whatever the on-disk state happened to be at
04:00, and that sometimes replays cleanly from the WAL and sometimes does not.

Revisit when either is true: a warrig container holds data not reproducible
from somewhere else, or a restore is attempted and the torn state bites. Until
then the cost (roughly eight dump hooks, and a multi-minute before-hook stall
on the same gastown target citadel is pushing to an hour earlier) outweighs it.

## Invocation

Both hosts set `ansible_become: true` in host_vars, and neither has passwordless
sudo, so **every run needs `--ask-become-pass`, including `--check`**:

```sh
# Dry-run
ansible-playbook --check --diff --ask-become-pass playbooks/configure_borgmatic.yml

# Apply (both sources; narrow with -e borgmatic_target=citadel or =warrig)
ansible-playbook --ask-become-pass playbooks/configure_borgmatic.yml
```

One caveat on `--check`: the SSH keygen task carries `check_mode: false` so
later tasks have a real file to slurp. A check run therefore does write one
inert, not-yet-authorized keypair to `/root/.ssh/borg_gastown`. It is not
read-only.

The vault password file is auto-resolved via `ansible.cfg`'s
`vault_password_file = .vault_pass`. Pass `--vault-password-file` if running
from elsewhere.

`inventory/hosts` carries everything the playbooks need: citadel and warrig in
`[borgmatic_source]` (which binds `group_vars/borgmatic_source/vault.yml`), and
gastown reachable in the same run, because the role delegates its pubkey
install there by inventory name and gastown's Python 3.12 pin lives in its
host_vars.

Verify without touching the repo:

```sh
sudo /usr/local/bin/borgmatic-export-system-state.sh
ls -la /home/nux/.system-state/
sudo borgmatic --config /etc/borgmatic/config.yaml create --dry-run -v 1
```

## Before first apply on a new host (one-time)

Add the host's passphrase to the vault and name it in host_vars:

```sh
ansible-vault edit inventory/group_vars/borgmatic_source/vault.yml
# add:  vault_borgmatic_<host>_passphrase: <that repo's passphrase>
```

```yaml
# inventory/host_vars/<host>.yml
borgmatic_passphrase_var: vault_borgmatic_<host>_passphrase
```

There is **no default** for `borgmatic_passphrase_var`, on purpose: a
wrong-but-defined value would silently write one host's passphrase into
another's file. The role asserts and fails with these instructions instead.
Stash a copy in KeePassXC at the same time. Losing the passphrase makes the
archives unrecoverable.

## Sharp edges observed on the live hosts

### warrig's ssh identity was the rsnapshot key (cut over 2026-08-18)

warrig's nightly push authenticated as root using **nux's rsnapshot key**, via
`ssh_command: ssh -i /home/nux/.ssh/rsnapshot_key` in the hand-managed config.
Nothing advertised that dependency, and rsnapshot is being retired, so anyone
tidying up `rsnapshot_key` on either end would have silently killed warrig's
backups. The role now creates a dedicated key and keeps `ssh_command` only to
force `BatchMode=yes`, with no `-i`. Rollback lives in
`/etc/borgmatic/config.yaml.pre-ansible.bak`, which still holds the old line.

### The ordering bug, 2026-08-18

The `authorized_key` task used to be a second play at the end of the playbook,
which made a first run impossible. Partway through play 1 the role pins root's
ssh to `borg_gastown` with `IdentitiesOnly yes`; the repo probe then dials
gastown, which had never seen that key. A rejected pubkey should fail fast. It
did not: ansible connects with `-tt`, sshd fell back to a password prompt, and
`borg` sat on it: 29 minutes with no `borg serve` ever starting on the gastown
side. Three fixes, all in place: the task moved in-role with `delegate_to` and
`become: false` before anything reaches across; `ssh_command` sets
`BatchMode=yes`; the probe is wrapped in `timeout` and rc 124 means "could not
tell", not "no repo".

### Hand-made cron lines must be deleted by hand

The `cron:` module keys on its own comment marker and cannot adopt an unmarked
line, so a hand-made entry at the same minute survives and the host ends up
with two nightly runs against one repo. borg's repo lock fails one of them,
which fires the urgent ntfy, and the `flock` in `borgmatic-run.sh` does not
help because the old line calls `borgmatic` directly. warrig's
`00 04 * * * /usr/bin/borgmatic --verbosity 1 --list --stats` was deleted
before its cutover. Check `sudo crontab -l | grep -c borgmatic` = 1 before and
after any apply.

### warrig's repo is keyfile mode, and the key is not backed up

```
Encrypted: Yes (key file BLAKE2b)
Key file:  /root/.config/borg/keys/volume1_NetBackup_borg_backup_warrig
```

| | encryption mode | key lives in | passphrase alone restores? |
|---|---|---|---|
| citadel | `repokey` | the repository on gastown | yes |
| warrig | `keyfile` | `/root/.config/borg/keys/` on warrig | **no** |

`/root` is not in `source_directories`, so warrig's key is in no backup. Until
a copy exists, warrig's backup only survives failures that leave warrig's root
filesystem intact. A dead disk takes the archives with it, and they look
perfectly healthy on gastown the whole time. borg 1.4.5 cannot convert the repo
in place (`key change-location` is borg 2.x).

Export it, put the export in KeePassXC, then remove the temp file. The export is
itself encrypted with the repo passphrase:

```sh
sudo BORG_PASSCOMMAND='cat /etc/borgmatic/passphrase' \
     borg key export --remote-path /usr/local/bin/borg \
     ssh://nux@gastown.shadeking.cam.local/volume1/NetBackup/borg_backup_warrig \
     /root/borg-warrig-key.txt
```

A paper copy is worth having too (`borg key export --paper`), since it survives
failure modes a password manager does not.

## Known gaps, not addressed here

- **No logrotate for `/var/log/borgmatic.log`.** It grows without bound. A
  drop-in in `/etc/logrotate.d/` would close it. This is also why warrig's
  `borgmatic_run_flags` keeps `--stats` but drops `--list`.
- **warrig's container databases are backed up as live files, not dumps.** See
  above.
- **The off-site and firebox-USB tier** (Phase 2 in the strategy doc) and the
  gastown rsnapshot pull tier (separate `rsnapshot` role) are elsewhere.

## Upstream

Automates [borgmatic](https://torsion.org/borgmatic/) (GPL-3.0-or-later), a
configuration frontend for [BorgBackup](https://www.borgbackup.org/)
(BSD-3-Clause). Notifications go through [ntfy](https://github.com/binwiederhier/ntfy)
(Apache-2.0 or GPL-2.0). This role installs and configures them from the
distro packages; no upstream code is vendored here.
