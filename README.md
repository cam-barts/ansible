# Home Ansible

Ansible repo for managing my home fleet: a Synology NAS (gastown), an
EndeavourOS server (citadel), an EndeavourOS workstation (warrig), three
Raspberry Pis, and the occasional HackTheBox PwnBox.

Companion to my [dotfiles repo](https://github.com/cam-barts/.dotfiles) — this
repo handles host-level config (services, backups, firewall, OS upgrades);
dotfiles handles per-user shell/editor setup.

## Layout

```
playbooks/   one entry-point per task (configure_*, deploy_*, upgrade_*)
roles/       reusable units called by the playbooks
inventory/   one inventory file per playbook, plus group_vars/host_vars
```

`ansible.cfg` pins `roles_path = ./roles` and
`vault_password_file = .vault_pass` (the latter is gitignored — you need to
drop your own vault password in there before running anything that touches an
encrypted var).

## Setup

```sh
ansible-galaxy collection install -r collections/requirements.yml
```

Pulls in `community.general`, `community.docker`, and `ansible.posix` — the
non-builtin collections the roles depend on (Docker container management,
`authorized_key`, `firewalld`, etc.).

## Playbooks

| Playbook | Hosts | Purpose |
| --- | --- | --- |
| `configure_borgmatic_citadel.yml` | citadel + gastown | Installs borgmatic on citadel, renders config + dump/cleanup/system-state hook scripts, generates an SSH key, authorizes it on gastown, initialises the repo, and schedules the daily 03:00 push. See `roles/borgmatic_citadel/README.md`. |
| `configure_rsnapshot.yml` | gastown (Synology) | Manages SynoCommunity rsnapshot on DSM 7.x: renders `rsnapshot.conf` outside `SYNOPKG_PKGDEST` (so package upgrades can't wipe it), installs a wrapper that emits proper start/ok/fail ntfy notifications, and rewrites the synoschedtask cron entries to call it. See `roles/rsnapshot_gastown/README.md`. |
| `configure_backup_targets.yml` | rsnapshot source hosts (Pis + warrig + citadel) | Ensures `rsync` (and anything else every backup source needs) is installed via the right package manager. |
| `deploy_node_exporter.yml` | Pis + EndeavourOS hosts | Installs and enables `prometheus-node-exporter` from the distro repo. Branches on `ansible_facts.os_family` for apt vs pacman. |
| `configure_ufw.yml` | Raspberry Pis | Default deny in / allow out, rate-limited SSH, full access from the workstation, node_exporter from citadel. |
| `configure_firewalld.yml` | warrig + citadel | Best-effort firewalld rule model (SSH plus the citadel-only node_exporter/cadvisor scrape ports), authored blind — no sudo to read the live ruleset, so Cam must reconcile against `firewall-cmd --list-all` on first apply. See `roles/firewalld/README.md`. |
| `configure_docker_log_rotation.yml` | Raspberry Pis (any host via `-e docker_daemon_target=`) | Manages the full `/etc/docker/daemon.json` per host: json-file 10 MB × 3 on the Pis, the loki-driver configs on warrig/citadel via their host_vars. Never restarts Docker on its own (`docker_daemon_allow_restart` defaults to `false`); it warns instead. |
| `deploy_cadvisor.yml` | `[cadvisor]` (Pis; warrig/citadel foldable via host_vars) | Deploys cAdvisor as a Docker container for per-container CPU/memory metrics, scraped by Prometheus on citadel. Guards against the loki-driver container-recreate deadlock on warrig/citadel. See `roles/cadvisor/README.md`. |
| `deploy_promtail.yml` | `[promtail]` (Pis) | Deploys Promtail as a Docker container shipping container logs plus host syslog/auth/kernel/dpkg logs to Loki on citadel. See `roles/promtail/README.md`. |
| `deploy_ntfy_server.yml` | black-pearl | Deploys ntfy as a Docker container — the server every alert in the lab (borgmatic, rsnapshot, dsm_config_audit, ...) flows through. See `roles/ntfy_server/README.md`. |
| `configure_ntfy_client.yml` | all hosts | Renders `/etc/ntfy/client.yml` so every host's `ntfy` CLI points at the self-hosted server instead of silently defaulting to the public `ntfy.sh`. See `roles/ntfy_client/README.md`. |
| `configure_borgmatic_warrig.yml` | warrig + gastown | warrig's counterpart to `configure_borgmatic_citadel.yml`: installs borgmatic, renders config + hook scripts (adopting a previously hand-managed `/etc/borgmatic/config.yaml`), generates an SSH key, authorizes it on gastown, and schedules the daily 04:00 push. First run must be `--check --diff`. See `roles/borgmatic_warrig/README.md`. |
| `configure_dsm_config_audit.yml` | gastown (Synology) | Drops a read-only DSM config-drift export script in `nux`'s home and pins the Windmill runner's SSH key to it with a forced `command=`, so a nightly Windmill flow can snapshot config-drift surfaces. See `roles/dsm_config_audit/README.md`. |
| `prune_docker.yml` | Raspberry Pis | One-shot `docker system prune` covering containers, non-dangling images, networks, volumes, and builder cache. |
| `upgrade_raspberrypi.yml` | Raspberry Pis | Serial Debian point-release upgrade: backs up state, rewrites apt sources to the next release, runs the upgrade, makes sure networking comes back, reboots, and verifies. Driven by a `release_map` in `roles/dist_upgrade/vars/`. |
| `configure_pwnbox.yml` | localhost | PwnBox bootstrap: installs the `package_list` from `common`, then clones dotfiles, stows them, and installs Starship + Atuin. |
| `pull/local.yml` | Raspberry Pis (via `ansible-pull`) | ansible-pull entrypoint importing the six Pi-relevant playbooks (node_exporter, cadvisor, docker log rotation, ufw, ntfy_client, backup_targets) in one shot. See "Pull mode (future)" below. |

## Roles

One role per playbook, described in the Playbooks table above. The complex
ones (`borgmatic_*`, `rsnapshot_gastown`, `dsm_config_audit`, `cadvisor`,
`promtail`, `ntfy_server`, `firewalld`, `docker_log_rotation`) carry their own
README; every role's tunables live in its commented `defaults/main.yml` —
that file is the variable reference, deliberately not duplicated here.

## Inventory

One canonical inventory: `inventory/hosts` (the `ansible.cfg` default, so no
`-i` needed). Base groups are `[raspberrypis]`, `[archlinux]` (citadel +
warrig), `[synology]` (gastown), and `[pwnbox]`; the role/playbook target
groups (`node_exporter`, `cadvisor`, `promtail`, `ntfy_server`, `firewalld`,
`backup_targets`, `borgmatic_source`) are built from those via `:children`.
Per-host connection details (IPs, become, gastown's SynoCommunity Python 3.12
interpreter pin) live in `inventory/host_vars/<name>.yml`; secrets in
`inventory/group_vars/*/vault.yml` (ansible-vault).

Blast radius comes from each playbook's `hosts:` pattern — add `--limit` to
narrow further.

## Running

Most playbooks are invoked directly:

```sh
ansible-playbook playbooks/<playbook>.yml
# add --check --diff for a dry run
```

The PwnBox is the exception — it's bootstrapped via `ansible-pull` so a fresh
HTB box can configure itself:

```sh
# -l pwnbox: the host is named "pwnbox" (not 127.0.0.1), so ansible-pull's
# implicit hostname/localhost limit won't match it on an HTB box.
ansible-pull -U https://github.com/cam-barts/ansible.git \
    -d ~/ansible-pwnbox -i inventory/ -l pwnbox -C main \
    playbooks/configure_pwnbox.yml
```

## Pull mode (future)

`pull/local.yml` is the `ansible-pull` entrypoint for the Raspberry Pi fleet —
it imports the six Pi-relevant playbooks (node_exporter, cadvisor, docker log
rotation, ufw, ntfy_client, backup_targets) so one pull run configures a Pi
end to end:

```sh
ansible-pull -U https://github.com/cam-barts/ansible.git -C stable \
    -i inventory/ pull/local.yml
```

Convention is to cut a `stable` branch for pull to track, rather than pulling
`main` directly. Two phases are not implemented yet: a canary systemd timer on
billy-of-tea, then rollout to the rest of the fleet.

## Vault

`ansible.cfg` resolves `vault_password_file = .vault_pass` automatically, so
once that file exists locally, encrypted vars decrypt transparently. Encrypted
files live alongside their group/host under `inventory/group_vars/<group>/vault.yml`
or `inventory/host_vars/<host>/vault.yml`.

To create or edit one:

```sh
ansible-vault create inventory/group_vars/<group>/vault.yml
ansible-vault edit   inventory/group_vars/<group>/vault.yml
```

## Host groups (what's actually out there)

- **Raspberry Pis** — `wellerman`, `black-pearl`, `billy-of-tea`. Raspbian, ARM. Run Docker workloads, get backed up to gastown via rsnapshot.
- **citadel** — EndeavourOS server (reports `os_family: Archlinux`). Runs the heavier docker stack; backed up via borgmatic to gastown.
- **warrig** — EndeavourOS workstation (reports `os_family: Archlinux`). Backup source for gastown's rsnapshot tier; also pushes its own daily borgmatic backup to gastown.
- **gastown** — Synology DSM 7.x. Backup destination for both the rsnapshot tier (pull) and the borgmatic tier (push from citadel).
- **PwnBox** — HackTheBox Parrot VM. Configured via `ansible-pull`.
