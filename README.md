# Home Ansible

Ansible repo for managing my home fleet: a Synology NAS (gastown), an Arch
server (citadel), an Arch workstation (warrig), three Raspberry Pis, and the
occasional HackTheBox PwnBox.

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

## Playbooks

| Playbook | Hosts | Purpose |
| --- | --- | --- |
| `configure_borgmatic_citadel.yml` | citadel + gastown | Installs borgmatic on citadel, renders config + dump/cleanup/pkglist hook scripts, generates an SSH key, authorizes it on gastown, initialises the repo, and schedules the daily 03:00 push. See `roles/borgmatic_citadel/README.md`. |
| `configure_rsnapshot.yml` | gastown (Synology) | Manages SynoCommunity rsnapshot on DSM 7.x: renders `rsnapshot.conf` outside `SYNOPKG_PKGDEST` (so package upgrades can't wipe it), installs a wrapper that emits proper start/ok/fail ntfy notifications, and rewrites the synoschedtask cron entries to call it. See `roles/rsnapshot_gastown/README.md`. |
| `configure_backup_targets.yml` | rsnapshot source hosts (Pis + warrig + citadel) | Ensures `rsync` (and anything else every backup source needs) is installed via the right package manager. |
| `deploy_node_exporter.yml` | Pis + Arch hosts | Installs and enables `prometheus-node-exporter` from the distro repo. Branches on `ansible_facts.os_family` for apt vs pacman. |
| `configure_ufw.yml` | Raspberry Pis | Default deny in / allow out, rate-limited SSH, full access from the workstation, node_exporter from citadel, plus any per-host extras via `ufw_allow_from_citadel`. |
| `configure_docker_log_rotation.yml` | Raspberry Pis | Merges a json-file log driver block (10 MB × 3 files) into `/etc/docker/daemon.json` and restarts Docker. Reads existing config first so it doesn't clobber other settings. |
| `prune_docker.yml` | (any Docker host) | One-shot `docker system prune` covering containers, non-dangling images, networks, volumes, and builder cache. |
| `upgrade_raspberrypi.yml` | Raspberry Pis | Serial Debian point-release upgrade: backs up state, rewrites apt sources to the next release, runs the upgrade, makes sure networking comes back, reboots, and verifies. Driven by a `release_map` in `roles/dist_upgrade/vars/`. |
| `remove_kubernetes.yml` | Raspberry Pis | Tears down kubeadm/kubelet/kubectl plus the apt repo, keyring, and `/etc/kubernetes` + `/var/lib/kubelet`. Cleanup left over from a previous experiment. |
| `configure_pwnbox.yml` | localhost | PwnBox bootstrap: installs the `package_list` from `common`, then clones dotfiles, stows them, and installs Starship + Atuin. |

## Roles

- **borgmatic_citadel** — borgmatic install, config render, SSH key, repo init, hook scripts, root cron. Dedicated README in the role.
- **rsnapshot_gastown** — SynoCommunity rsnapshot config + wrapper + synoschedtask rewrite. Dedicated README in the role.
- **backup_targets** — package install for hosts that gastown pulls from; dispatches apt vs pacman.
- **node_exporter** — installs and enables `prometheus-node-exporter`.
- **ufw** — installs ufw and applies the home firewall ruleset.
- **docker_log_rotation** — merges json-file log limits into `daemon.json`.
- **remove_kubernetes** — purges a kubeadm install.
- **dist_upgrade** — Debian release upgrade flow (backup → rewrite sources → upgrade → networking → reboot → verify).
- **common** — apt-only base package install loop (`package_list`).
- **pwnbox** — dotfiles + Starship + Atuin layered on top of `common`.

## Inventories

One file per playbook so each can be invoked with `-i inventory/<name>` without
pulling in unrelated host vars:

| File | Used by |
| --- | --- |
| `inventory/borgmatic_citadel` | `configure_borgmatic_citadel.yml` (citadel + gastown together). |
| `inventory/synology` | `configure_rsnapshot.yml`. Pins `ansible_python_interpreter` to the SynoCommunity Python 3.12 because DSM's stock Python is 3.8. |
| `inventory/backup_targets` | `configure_backup_targets.yml`. `[pis]` + `[arch_backup_targets]` (warrig, citadel) under `[backup_targets:children]`. |
| `inventory/node_exporter` | `deploy_node_exporter.yml`. `[raspberrypis]` + `[archlinux]` under `[node_exporter:children]`. |
| `inventory/raspberrypis` | `configure_ufw.yml`, `configure_docker_log_rotation.yml`, `upgrade_raspberrypi.yml`, `remove_kubernetes.yml`. |
| `inventory/pwnbox` | `configure_pwnbox.yml`. Default inventory in `ansible.cfg`. |
| `inventory/group_vars/`, `inventory/host_vars/` | Per-group / per-host overrides, including ansible-vault encrypted secrets. |

## Running

Most playbooks are invoked directly:

```sh
ansible-playbook -i inventory/<file> playbooks/<playbook>.yml
# add --check --diff for a dry run
```

The PwnBox is the exception — it's bootstrapped via `ansible-pull` so a fresh
HTB box can configure itself:

```sh
ansible-pull -U https://github.com/cam-barts/ansible.git \
    -d ~/ansible-pwnbox -i pwnbox -C main \
    playbooks/configure_pwnbox.yml
```

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
- **citadel** — Arch server. Runs the heavier docker stack; backed up via borgmatic to gastown.
- **warrig** — Arch workstation. Backup source for gastown's rsnapshot tier.
- **gastown** — Synology DSM 7.x. Backup destination for both the rsnapshot tier (pull) and the borgmatic tier (push from citadel).
- **PwnBox** — HackTheBox Parrot VM. Configured via `ansible-pull`.
