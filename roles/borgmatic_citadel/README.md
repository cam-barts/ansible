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
   - `borgmatic-dump-dbs.sh` — `before: action` hook; `docker exec` pg_dumpall
     + mariadb-dump into `/var/backups/borgmatic-dumps/`. Serialised with
     `flock` on the dump dir, `umask 077`, split inner/outer `timeout`, and a
     terminator check so a truncated dump is never promoted. Always exits 0.
   - `borgmatic-cleanup-dumps.sh` — `after: action` hook; wipes the dump dir on
     both success and failure, and asserts each DB left either a dump or a
     `DUMP-FAILED-<name>.txt` marker (catches a dump script that never ran).
   - `borgmatic-export-system-state.sh` — `before: action` hook; captures pacman
     state, pacman/mirror config, fstab, enabled units and host identity into
     `~/.system-state` so a restore can replay the host. Supersedes the older
     `borgmatic-export-pkglists.sh`, which the role removes.
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
  - /home/nux/docker
  - /home/nux/.config
  ...

borgmatic_databases:  # what to dump via docker exec
  - name: windmill-postgres
    dump_file: "windmill-postgres-all.sql"
    terminator: "PostgreSQL database cluster dump complete"
    dump_invocation: |-
      timeout "$outer_timeout" docker exec windmill-db-1 \
          timeout "$inner_timeout" pg_dumpall -U postgres
  - name: bunkerweb-mariadb
    ...
```

`dump_invocation` is rendered verbatim as the argv handed to the script's
`dump()` helper and references `$outer_timeout` / `$inner_timeout`, which come
from `borgmatic_dump_timeout_outer` (wraps `docker`, backstop for a wedged CLI)
and `borgmatic_dump_timeout_inner` (runs inside the container, actually bounds
the dump). Outer must stay larger than inner.

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
    --check --diff \
    playbooks/configure_borgmatic_citadel.yml

# Apply
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    playbooks/configure_borgmatic_citadel.yml
```

The vault password file is auto-resolved via `ansible.cfg`'s
`vault_password_file = .vault_pass`. Pass `--vault-password-file` if you're
running from elsewhere.

`inventory/hosts` carries everything the playbook needs: citadel in
`[borgmatic_source]` (binds the vault passphrase) and gastown in `[synology]`
(the receive end, used by the second play to install the SSH pubkey).

## Not handled here

- The gastown rsnapshot tier (separate `rsnapshot_gastown` role).
- Off-site / firebox-USB tier (Phase 2 in the strategy doc).
- Reconciling the exclude list against warrig's live config — this role's
  default list is the documented best-practice pattern plus citadel-specific
  Garage/Loki/Prom entries. If `sudo cat /etc/borgmatic/config.yaml` on
  warrig has extra entries not captured here, fold them in.
