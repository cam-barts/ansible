# borgmatic_warrig

Codifies warrig's daily borg push to gastown — the counterpart to
`borgmatic_citadel`. warrig runs at **04:00**, citadel at 03:00, so gastown is
never servicing two concurrent receivers.

---

## ⚠️ READ THIS BEFORE THE FIRST APPLY

**This role adopts a hand-managed config it has never been able to read.**

warrig's live `/etc/borgmatic/config.yaml` is root-only (`0600`) and was never
captured into git. Everything in `defaults/main.yml` is *reconstructed* from
`borgmatic_citadel` — which was itself written to mirror warrig — plus what
could be observed without sudo. It is **not** verified against the live file.

So the first pass is a reconnaissance run, not a config write (one deliberate
exception: the SSH keygen task runs even under `--check` so later tasks can
read the pubkey — it drops an inert, not-yet-authorized keypair at
`/root/.ssh/borg_gastown`):

```sh
ANSIBLE_ROLES_PATH=./roles ansible-playbook \
    --check --diff --ask-become-pass \
    playbooks/configure_borgmatic_warrig.yml
```

The `--diff` on the "Render borgmatic config" task prints the **live config**
as the "before" side. That output is the authority. Reconcile it into
`defaults/main.yml` — specifically:

| What | Why it matters |
|---|---|
| `exclude_patterns` | `/home/nux` is ~110G (`docker_services` alone is 76G). The defaults here are deliberately short; the live list is almost certainly longer. Getting this wrong bloats the repo on gastown. |
| `borgmatic_ntfy_topic` | Defaults to `warrig_events` by convention only. If live publishes elsewhere, alerts go to a topic nobody subscribes to — the one failure mode that fails **silently**. |
| `encryption_passcommand` | If live doesn't use `cat /etc/borgmatic/passphrase`, the passphrase plumbing here is wrong. |
| `source_directories` | Confirm `/etc` + `/home/nux` is the real set. |
| cron slot | 04:00 is inherited from citadel's role notes, not observed. `sudo crontab -l`. |

Only after the diff is reconciled should the apply run without `--check`.

The role does back itself up: `/etc/borgmatic/config.yaml.pre-ansible.bak` is
written once (`force: false`) before the template lands.

---

## One-time prerequisites

### 1. Vault variable — **REQUIRED, NOT PRESENT TODAY**

`inventory/group_vars/borgmatic_source/vault.yml` currently contains exactly
one key: `vault_borgmatic_passphrase` — that is **citadel's** repo passphrase.
warrig has a pre-existing repo with its own passphrase; reusing citadel's would
make warrig's archives unopenable.

Cam must add warrig's **existing** passphrase (this is an adoption, not a
rotation — pull the value from KeePassXC or from `sudo cat
/etc/borgmatic/config.yaml`):

```sh
ansible-vault edit inventory/group_vars/borgmatic_source/vault.yml
# add:
#   vault_borgmatic_warrig_passphrase: <warrig's existing passphrase>
```

The role asserts on this and fails with the same instructions. To run the
reconcile pass *before* adding it, pass `-e borgmatic_manage_passphrase=false`
— the assert and the passphrase write are both skipped.

### 2. Inventory

`inventory/hosts` carries everything: warrig is in `[borgmatic_source]`
alongside citadel, so `group_vars/borgmatic_source/vault.yml` binds to it
directly — no inventory stacking, no explicit `-e @vault` needed. gastown's
Python 3.12 interpreter pin lives in `host_vars/gastown.yml`.

### 3. Existing root crontab

The role adds a *named* cron entry. Any hand-made borgmatic line already in
root's crontab survives, giving warrig two daily runs. `borgmatic-run.sh` takes
a `flock`, but the old line probably invokes `borgmatic` directly and bypasses
it. Check `sudo crontab -l` and delete the hand-made line after the first
successful managed run.

---

## What it does

1. `pacman -S borgmatic borg` (no-op — both already present on warrig).
2. Renders `/etc/borgmatic/config.yaml` (root `0600`), gated by
   `borgmatic config validate -c %s` so a malformed template never lands.
3. Writes `/etc/borgmatic/passphrase` (root `0600`) from
   `vault_borgmatic_warrig_passphrase`; the config's `encryption_passcommand`
   cats it, so the secret is in neither the config nor git.
4. Generates `/root/.ssh/borg_gastown` (ed25519), renders a matching
   `Host gastown.shadeking.cam.local` block into `/root/.ssh/config` with
   `IdentitiesOnly yes`, and seeds known_hosts with one `BatchMode` probe.
   The playbook's **second play** installs that pubkey into `nux`'s
   `authorized_keys` on gastown — the two plays are a matched pair. Running
   only the first pins root's gastown ssh to a key gastown hasn't accepted yet.
   `-e borgmatic_manage_ssh_config=false` skips the ssh-config half if the
   existing hand-made trust should be left alone.
5. Installs `/usr/local/bin/borgmatic-export-system-state.sh` and wires it into
   the config's `before: action` hook (`|| true`, so a stumble in the export
   never aborts the backup). This **replaces** the manual `before_backup` edit
   the previous version of this README documented — no hand edits needed now.
6. Installs `/usr/local/bin/borgmatic-run.sh` — `flock`'d, with an explicit
   start/ok/fail ntfy chain so a partial pipe can't fire "ok" on a real failure.
7. Adopts (never re-inits) the existing repo: `borgmatic info` probes first, and
   `/var/lib/borgmatic/.initialized` guards afterwards.
8. Root cron: daily 04:00 → `borgmatic-run.sh`, logging to
   `/var/log/borgmatic.log`.

## Databases: none, deliberately

warrig runs a pile of containerised DBs (`immich_postgres`,
`atuin-postgresql-1`, `hoppscotch-postgres-1`, `plausible-plausible_db-1`,
`surrealdb`, `ansible-semaphore-postgres-1`, meilisearch, redis/valkey …).
Nothing in this repo or its git history indicates warrig ever dumped them, so
none are configured here — inventing ~8 dump hooks unasked is how a multi-minute
before-hook stall ends up overlapping citadel's 03:00 window on the same target.

`borgmatic_citadel` already has the full pattern (`borgmatic-dump-dbs.sh`,
`borgmatic-cleanup-dumps.sh`, split inner/outer `timeout`, terminator checks,
degraded-not-failed ntfy path) parameterised by a `borgmatic_databases` list.
Porting it here is a copy job if/when Cam wants it. **Open question for Cam.**

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

## Live state observed 2026-08-17 (no sudo)

- `borgmatic`, `borg`, `ntfy` all present at `/usr/bin/`.
- `borgmatic.timer` exists but is **disabled** — scheduling is via root cron
  (unreadable without sudo), consistent with what this role installs.
- `/usr/local/bin/` holds only `piactl` and `uvx` — the export script and run
  wrapper have never been applied.
- `/var/lib/borgmatic`, `/home/nux/.system-state`, `/var/log/borgmatic.log` do
  not exist yet. The last one means the live cron line logs somewhere else;
  worth confirming so two log paths don't diverge.
