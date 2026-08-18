# borgmatic_warrig

Codifies warrig's daily borg push to gastown, the counterpart to
`borgmatic_citadel`. warrig runs at 04:00, citadel at 03:00, so gastown is
never servicing two concurrent receivers.

---

## Reconciled 2026-08-18

This role adopts a config that was hand-managed for years. That config has now
been read and folded into `defaults/main.yml`, so the values here are observed
rather than guessed.

What the reconcile found:

| What | Guess | Live | Outcome |
|---|---|---|---|
| ntfy topic | `warrig_events` | `[redacted-vaulted-topic]` | **Corrected.** The guess would have sent every alert to a topic with no subscriber, which is the one failure mode that fails silently. |
| ssh identity | none set | `ssh_command: ssh -i /home/nux/.ssh/rsnapshot_key` | **Cutover.** See below. |
| `source_directories` | `/etc`, `/home/nux` | `/home/nux/`, `/etc/`, `/usr/local/` | `/usr/local/` had been missed. Adopted verbatim. |
| `exclude_patterns` | 10 invented | 22 real | Live list adopted whole, plus three additions. |
| passphrase | assumed a passcommand | literal `encryption_passphrase:` | Role moves it to `/etc/borgmatic/passphrase` (0600) behind a passcommand. |
| cron slot | 04:00, inferred | 04:00, confirmed | Confirmed twice: root's crontab, and the Aug 17 04:08 mtime on gastown's repo index. |
| `checkpoint_interval` | not modelled | 600 | Carried forward. |
| compression, retention, checks | copied from citadel | identical | No change. |
| config schema | 2.x flat | 1.x sectioned | The apply is also a schema migration. Validated against warrig's borgmatic 2.1.6: `All configuration files are valid`. |

The role backs itself up regardless: `/etc/borgmatic/config.yaml.pre-ansible.bak`
is written once (`force: false`) before the template lands, and it keeps the
working `ssh_command` line for rollback.

### The ssh identity cutover

warrig's nightly push has been authenticating as root using **nux's rsnapshot
key**, via `ssh_command` in the borgmatic config. Nothing in the old setup
advertises that dependency, and rsnapshot is being retired from this repo, so
anyone tidying up `rsnapshot_key` on either end would silently kill warrig's
backups.

This role creates a dedicated `/root/.ssh/borg_gastown`, authorizes it on
gastown, and points root's ssh at it with `IdentitiesOnly`. It keeps
`ssh_command` only to force `BatchMode=yes`, with no `-i`, so identity
selection stays in `/root/.ssh/config`.

That is a real authentication change on a working backup, and `/root/.ssh` on
warrig held nothing but `known_hosts` beforehand, so there is no fallback
identity. Verify by hand before trusting the next 04:00:

```sh
sudo borgmatic --config /etc/borgmatic/config.yaml repo-info
```

### The ordering bug this hit on the first run, 2026-08-18

The pubkey install used to be a second play at the end of the playbook. That
made a successful first run impossible, and the failure was silent rather than
loud.

Partway through play 1 the role pins root's ssh to `borg_gastown` with
`IdentitiesOnly yes`. The repo probe a few tasks later then dials gastown,
which had never seen that key, because play 2 had not run yet. A rejected
pubkey should fail fast. It did not: ansible connects with `-tt`, so the whole
chain has a TTY, sshd fell back to a password prompt, and `borg` sat on that
prompt. The run hung for 29 minutes with no `borg serve` process ever starting
on the gastown side, which is what gave it away.

Three changes came out of it, all in place now:

- The `authorized_key` task moved into the role, `delegate_to` gastown with
  `become: false`, positioned before anything reaches across. The playbook is
  a single play and `configure_borgmatic_warrig.yml` no longer has a play 2.
- `ssh_command` sets `BatchMode=yes`, so a rejected key is an immediate error
  rather than a prompt.
- The probe is wrapped in `timeout {{ borgmatic_probe_timeout }}` as a
  backstop, and the init task now treats rc 124 as "could not tell" instead of
  "no repo". Without that a stuck probe would have sent the role off to
  initialize a repository that already holds every backup warrig has.

### Optional verification pass

The reconcile is done, so a check run is now a verification rather than a
discovery, and should come back quiet:

```sh
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    --check --diff --ask-become-pass \
    playbooks/configure_borgmatic_warrig.yml
```

One caveat: the SSH keygen task carries `check_mode: false` so later tasks can
read the pubkey. A check run therefore does write an inert, not-yet-authorized
keypair to `/root/.ssh/borg_gastown`.

---

## One-time prerequisites, all satisfied 2026-08-18

Kept here because they apply again to any rebuild of warrig, not because
anything is outstanding.

### 1. Vault variable (done)

`inventory/group_vars/borgmatic_source/vault.yml` held exactly one key,
`vault_borgmatic_passphrase`, and that is citadel's repo passphrase. warrig has
a pre-existing repo with its own, and reusing citadel's would have made warrig's
archives unopenable.

`vault_borgmatic_warrig_passphrase` was added with warrig's existing value, an
adoption rather than a rotation. The live config stored it as a literal under
`storage:`, so that file was the authority:

```sh
sudo grep encryption_passphrase /etc/borgmatic/config.yaml
ansible-vault edit inventory/group_vars/borgmatic_source/vault.yml
```

The role asserts on it and fails with those instructions. To run a pass without
it, `-e borgmatic_manage_passphrase=false` skips both the assert and the write.

### 2. Inventory (nothing to do)

`inventory/hosts` carries everything: warrig is in `[borgmatic_source]`
alongside citadel, so `group_vars/borgmatic_source/vault.yml` binds to it
directly, with no inventory stacking and no explicit `-e @vault` needed.
gastown's Python 3.12 interpreter pin lives in `host_vars/gastown.yml`, and the
role delegates its pubkey install to gastown by inventory name, so gastown has
to be reachable in the same run.

### 3. The old root cron line (deleted)

Root's crontab held this hand-made line:

```crontab
00 04 * * * /usr/bin/borgmatic --verbosity 1 --list --stats
```

The `cron:` module keys on its own comment marker and cannot adopt an unmarked
line, so applying this role adds a second entry at the same minute rather than
replacing that one. Two borgmatic processes would hit the same gastown repo
simultaneously, borg's repo lock would fail one of them, and that fires the
urgent ntfy. The `flock` in `borgmatic-run.sh` does not help here, because the
old line calls `borgmatic` directly and never takes the lock.

It was deleted before the cutover. The other entry, `00 */6 * * * updatedb`, is
unrelated and stays.

---

## What it does

1. `pacman -S borgmatic borg`, a no-op since both are already present on warrig.
2. Renders `/etc/borgmatic/config.yaml` (root `0600`), gated by
   `borgmatic config validate -c %s` so a malformed template never lands.
3. Writes `/etc/borgmatic/passphrase` (root `0600`) from
   `vault_borgmatic_warrig_passphrase`. The config's `encryption_passcommand`
   cats it, so the secret is in neither the config nor git.
4. Generates `/root/.ssh/borg_gastown` (ed25519), installs that pubkey into
   `nux`'s `authorized_keys` on gastown (`delegate_to`, so it happens before
   anything dials across), renders a matching
   `Host gastown.shadeking.cam.local` block into `/root/.ssh/config` with
   `IdentitiesOnly yes`, and seeds known_hosts with one `BatchMode` probe.
   `-e borgmatic_manage_ssh_config=false` skips the ssh-config half if the
   existing hand-made trust should be left alone.
5. Installs `/usr/local/bin/borgmatic-export-system-state.sh` and wires it into
   the config's `before: action` hook (with `|| true`, so a stumble in the
   export never aborts the backup). This replaces the manual `before_backup`
   edit the previous version of this README documented; no hand edits now.
6. Installs `/usr/local/bin/borgmatic-run.sh`, `flock`'d, with an explicit
   start/ok/fail ntfy chain so a partial pipe can't fire "ok" on a real failure.
7. Adopts, never re-inits, the existing repo: `borgmatic info` probes first, and
   `/var/lib/borgmatic/.initialized` guards afterwards.
8. Root cron: daily 04:00 runs `borgmatic-run.sh`, logging to
   `/var/log/borgmatic.log`.

## Databases: none, deliberately

warrig runs a pile of containerised DBs (`immich_postgres`,
`atuin-postgresql-1`, `hoppscotch-postgres-1`, `plausible-plausible_db-1`,
`surrealdb`, `ansible-semaphore-postgres-1`, meilisearch, redis/valkey, and
more). Nothing in this repo or its git history indicates warrig ever dumped
them, so none are configured here. Inventing eight or so dump hooks unasked is
how a multi-minute before-hook stall ends up overlapping citadel's 03:00 window
on the same target.

`borgmatic_citadel` already has the full pattern (`borgmatic-dump-dbs.sh`,
`borgmatic-cleanup-dumps.sh`, split inner/outer `timeout`, terminator checks,
degraded-not-failed ntfy path) parameterised by a `borgmatic_databases` list.
Porting it here is a copy job.

The decision, made 2026-08-18 and not merely deferred: warrig keeps the
pre-Ansible behaviour. Its container data directories ride along inside
`/home/nux` as ordinary files, copied mid-write. That is what warrig has always
done, so this role changes nothing, but it is worth being honest about what it
buys. A file-level copy of a running Postgres restores to whatever the on-disk
state happened to be at 04:00. Sometimes that replays cleanly from the WAL and
sometimes it does not.

Revisit when either of two things is true: a warrig container holds data that
is not reproducible from somewhere else, or a restore is actually attempted and
the torn state bites. Until then the cost (roughly eight dump hooks, and a
multi-minute before-hook stall on the same gastown target citadel is pushing to
an hour earlier) outweighs it.

## Apply

```sh
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    --ask-become-pass \
    playbooks/configure_borgmatic_warrig.yml
```

Verify without touching the repo:

```sh
sudo /usr/local/bin/borgmatic-export-system-state.sh
ls -la /home/nux/.system-state/
sudo borgmatic --config /etc/borgmatic/config.yaml create --dry-run -v 1
```

## Live state observed 2026-08-18

- `borgmatic` 2.1.6, `borg` 1.4.5, `ntfy` 2.27.0, all at `/usr/bin/`.
- The backup is healthy and running. gastown's
  `/volume1/NetBackup/borg_backup_warrig` was last written Aug 17 04:08, with a
  122 MB index.
- `borgmatic.timer` exists but is disabled, and `journalctl -t borgmatic` is
  empty, so root cron owns the schedule. Confirmed.
- `/var/log/borgmatic.log` does not exist, because the live cron line redirects
  nowhere and its output goes to cron mail. This role's cron entry creates it.
  Nothing rotates it yet, which is why `borgmatic_run_flags` drops `--list`.
- `/var/lib/borgmatic` and `/home/nux/.system-state` do not exist yet, and
  `/usr/local/bin` has neither the export script nor the run wrapper, so no
  part of this role has ever been applied.
- gastown's `authorized_keys` for `nux` holds 7 keys, including one commented
  `nux@Warrig`. The second play adds an 8th, commented
  `borgmatic warrig→gastown`.

## The repo is keyfile mode, and the key is not backed up

Discovered 2026-08-18 from the first successful `repo-info`:

```
Encrypted: Yes (key file BLAKE2b)
Key file:  /root/.config/borg/keys/volume1_NetBackup_borg_backup_warrig
```

warrig and citadel differ here, and only warrig has the problem:

| | encryption mode | key lives in | passphrase alone restores? |
|---|---|---|---|
| citadel | `repokey` | the repository on gastown | yes |
| warrig | `keyfile` | `/root/.config/borg/keys/` on warrig | **no** |

`/root` is not in `source_directories`, so warrig's key is in no backup. Storing
it inside the archives it unlocks would be circular, and storing it on gastown
would put the key next to the ciphertext. It has to live somewhere else.

Until that copy exists, warrig's backup only survives failures that leave
warrig's root filesystem intact. A dead disk takes the archives with it, and
they will look perfectly healthy on gastown the whole time.

borg 1.4.5 cannot convert the repo in place. `key change-location` is borg 2.x,
and `key migrate-to-repokey` is the old Attic passphrase migration, not this.

### Capture the key

Export it, put the export in KeePassXC, then remove the temp file. The export
is itself encrypted with the repo passphrase, so it plus the vault entry are
what a restore needs.

```sh
sudo BORG_PASSCOMMAND='cat /etc/borgmatic/passphrase' \
     borg key export --remote-path /usr/local/bin/borg \
     ssh://nux@gastown.shadeking.cam.local/volume1/NetBackup/borg_backup_warrig \
     /root/borg-warrig-key.txt
```

A paper copy is worth having too, since it survives the failure modes a
password manager does not:

```sh
sudo BORG_PASSCOMMAND='cat /etc/borgmatic/passphrase' \
     borg key export --paper --remote-path /usr/local/bin/borg \
     ssh://nux@gastown.shadeking.cam.local/volume1/NetBackup/borg_backup_warrig
```

When warrig eventually moves to borg 2.x, `borg key change-location repokey`
brings it in line with citadel and makes all of this unnecessary.

## Known gaps, not addressed here

- **No logrotate for `/var/log/borgmatic.log`.** With `--stats` and no
  `--list` it grows slowly, but it grows without bound. A drop-in in
  `/etc/logrotate.d/` would close it.
- **Container databases are backed up as live files, not dumps.** immich (25G),
  hoarder (12G), surrealdb (7.8G) and the rest sit inside `/home/nux` and get
  copied mid-write. That matches the pre-Ansible behaviour exactly, so this
  role does not change it, but a file-level copy of a running Postgres is not a
  restorable database. See the section below.

## Upstream

Automates [borgmatic](https://torsion.org/borgmatic/) (GPL-3.0-or-later), a
configuration frontend for [BorgBackup](https://www.borgbackup.org/)
(BSD-3-Clause). Notifications go through [ntfy](https://github.com/binwiederhier/ntfy)
(Apache-2.0 or GPL-2.0). This role installs and configures them from the
distro packages; no upstream code is vendored here.
