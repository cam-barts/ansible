# borgmatic_citadel

Codifies the second backup tier from `~/silverbullet/AI Generated/Barbossa/Backup Strategy Review.md`:
citadel pushes a daily borg archive to gastown at 03:00 (offset from
warrig's 04:00 so gastown isn't servicing two concurrent receivers).

## What it does

1. `pacman -S borgmatic borg` on citadel (no-op if already present).
2. Renders `/etc/borgmatic/config.yaml` (root 0600) from `defaults/main.yml`.
   `borgmatic --syntax-only` validates before the file lands.
3. Pulls `vault_borgmatic_passphrase` from `inventory/group_vars/borgmatic_source/vault.yml`
   (ansible-vault encrypted) and writes it to `/etc/borgmatic/passphrase`
   (root 0600). The config's `encryption_passcommand` cats that file, so the
   secret never lands in the rendered yaml.
4. Generates an ed25519 keypair at `/root/.ssh/borg_gastown` on citadel,
   renders a matching `Host gastown.shadeking.cam.local` block into
   `/root/.ssh/config`, and seeds the gastown known_hosts entry with one
   `BatchMode=yes` ssh probe.
5. A second play in the playbook delegates to gastown and installs the
   citadel pubkey into `nux`'s `~/.ssh/authorized_keys` via
   `ansible.posix.authorized_key`.
6. Runs `borgmatic init --encryption repokey-blake2` against the remote
   repo. Idempotent: `borgmatic info` probes first, and a marker file at
   `/var/lib/borgmatic/.initialized` guards the second run.
7. Installs four scripts under `/usr/local/bin/`:
   - `borgmatic-run.sh` — cron entry-point with explicit start/ok/fail ntfy chain.
   - `borgmatic-dump-dbs.sh` — `before_backup` hook; `docker exec` pg_dumpall
     + mariadb-dump into `/var/backups/borgmatic-dumps/`.
   - `borgmatic-cleanup-dumps.sh` — `after_backup` hook; wipes the dump dir.
   - `borgmatic-export-pkglists.sh` — `before_backup` hook; refreshes
     `/etc/pkg-explicit.list` and `/etc/pkg-foreign.list` so a restore can
     replay pacman state.
8. Installs a root cron entry: daily 03:00 → `borgmatic-run.sh`, logging to
   `/var/log/borgmatic.log`.

The role does NOT trigger a first manual `borgmatic create`. The first
scheduled 03:00 fire is the validation: the explicit start/ok/fail ntfy
chain surfaces any breakage immediately, and there's nothing the manual run
exercises that the cron path doesn't.

## Inputs

See `defaults/main.yml`. The two you're most likely to override per host:

```yaml
borgmatic_source_directories:  # what to capture
  - /etc
  - /home/nux/docker_services
  - /home/nux/.config
  ...

borgmatic_databases:  # what to dump via docker exec
  - name: windmill-postgres
    container: windmill-db-1
    dump_cmd: "docker exec windmill-db-1 pg_dumpall -U postgres"
    dump_file: "windmill-postgres-all.sql"
  - name: bunkerweb-mariadb
    ...
```

Excluded by default: caches, node_modules, venvs, Garage object/meta data
(S3 has internal replication — config is what's worth backing up), Loki +
Prometheus TSDB chunks (derivable), changedetection browser snapshots.

## Before first apply (one-time)

The role automates everything except the passphrase value itself, which Cam
must populate once into an ansible-vault file:

```sh
ansible-vault create inventory/group_vars/borgmatic_source/vault.yml
# add to the editor that opens:
#   vault_borgmatic_passphrase: <strong passphrase>
```

Stash a copy in KeePassXC at the same time — losing the passphrase makes the
archives unrecoverable. (Same hygiene as warrig.)

The `.vault_pass` file in the repo root (per `ansible.cfg`) decrypts it at
playbook run time.

## Invocation

```sh
# Dry-run
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    -i inventory/borgmatic_citadel --check --diff \
    playbooks/configure_borgmatic_citadel.yml

# Apply
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    -i inventory/borgmatic_citadel \
    playbooks/configure_borgmatic_citadel.yml
```

The vault password file is auto-resolved via `ansible.cfg`'s
`vault_password_file = .vault_pass`. Pass `--vault-password-file` if you're
running from elsewhere.

`inventory/borgmatic_citadel` defines the two groups this playbook needs:
`[citadel]` for the push source, `[gastown]` for the receive end (used by
the second play to install the SSH pubkey).

## Alternative secret stores

`encryption_passcommand: cat /etc/borgmatic/passphrase` is the simplest
option that works on a headless server. Two alternatives if Cam ever wants
to harden:

- **libsecret / secret-tool.** Requires a running keyring; awkward for
  root-cron. Skip unless citadel grows a user-session keyring.
- **Windmill-pulled.** Have a Windmill resource hold the passphrase and
  pull it via a Windmill API call from a small wrapper script. Cleaner
  rotation story; couples backup health to Windmill being up. Worth
  considering only if Cam wants a single source of truth for secrets
  across the fleet.

## Not handled here

- The gastown rsnapshot tier (separate `rsnapshot_gastown` role).
- Off-site / firebox-USB tier (Phase 2 in the strategy doc).
- Reconciling the exclude list against warrig's live config — this role's
  default list is the documented best-practice pattern plus citadel-specific
  Garage/Loki/Prom entries. If `sudo cat /etc/borgmatic/config.yaml` on
  warrig has extra entries not captured here, fold them in.
