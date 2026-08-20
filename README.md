# Home Ansible

Ansible repo for managing my home fleet: a Synology NAS (gastown), an
EndeavourOS server (citadel), an EndeavourOS workstation (warrig), three
Raspberry Pis, and the occasional HackTheBox PwnBox.

Companion to my [dotfiles repo](https://github.com/cam-barts/.dotfiles). This
repo handles host-level config (services, backups, firewall, OS upgrades);
dotfiles handles per-user shell and editor setup.

## Layout

```
playbooks/   one entry-point per task (configure_*, deploy_*, upgrade_*)
roles/       reusable units called by the playbooks
inventory/   the one canonical inventory (hosts), plus group_vars/host_vars
```

`ansible.cfg` pins `roles_path = ./roles` and
`vault_password_file = .vault_pass`. That second file is gitignored, so drop
your own vault password in there before running anything that touches an
encrypted var.

## Setup

```sh
ansible-galaxy collection install -r collections/requirements.yml
```

Pulls in `community.general`, `community.docker`, and `ansible.posix`, the
non-builtin collections the roles depend on (Docker container management,
`authorized_key`, `firewalld`, and so on).

## Playbooks

| Playbook | Hosts | Purpose |
| --- | --- | --- |
| `configure_borgmatic_citadel.yml` | citadel + gastown | Installs borgmatic on citadel, renders config + dump/cleanup/system-state hook scripts, generates an SSH key, authorizes it on gastown, initialises the repo, and schedules the daily 03:00 push. See `roles/borgmatic/README.md`. |
| `configure_rsnapshot.yml` | gastown (Synology) | Manages SynoCommunity rsnapshot on DSM 7.x: renders `rsnapshot.conf` outside `SYNOPKG_PKGDEST` (so package upgrades can't wipe it), installs a wrapper that emits proper start/ok/fail ntfy notifications, and rewrites the synoschedtask cron entries to call it. See `roles/rsnapshot/README.md`. |
| `configure_backup_targets.yml` | rsnapshot source hosts (Pis + warrig + citadel) | Ensures `rsync` (and anything else every backup source needs) is installed via the right package manager. |
| `deploy_node_exporter.yml` | Pis + EndeavourOS hosts | Installs and enables `prometheus-node-exporter` from the distro repo. Branches on `ansible_facts.os_family` for apt vs pacman. |
| `configure_ufw.yml` | Raspberry Pis | Default deny in / allow out, rate-limited SSH, full access from the workstation, node_exporter from citadel. |
| `configure_firewalld.yml` | warrig + citadel | Firewalld rule model: SSH, plus rich rules scoping warrig's node_exporter (9100) and cAdvisor (8080) to citadel. Reconciled against the live rulesets on both hosts and applied 2026-08-17, so it should now report zero changes. Additive only; it never removes a rule. See `roles/firewalld/README.md`. |
| `configure_docker_log_rotation.yml` | Raspberry Pis (any host via `-e docker_daemon_target=`) | Manages the full `/etc/docker/daemon.json` per host: json-file 10 MB × 3 on the Pis, the loki-driver configs on warrig/citadel via their host_vars. Never restarts Docker on its own (`docker_daemon_allow_restart` defaults to `false`); it warns instead. |
| `configure_compose_<stack>.yml` (×37) | the host(s) running that stack | One per compose stack in the lab (40 stacks; portainer and portainer_agent playbooks span hosts). Byte-faithfully restores the stack's compose/env files with vaulted secrets, then reconciles the containers to them: `up -d` by default (a down or wiped stack comes back), `-e compose_stack_state=absent` brings it down, `stopped`/`restarted` also work. Converged stacks report zero changes. See `roles/compose_stack/README.md`. |
| `deploy_cadvisor.yml` | `[cadvisor]` (Pis; warrig/citadel foldable via host_vars) | Deploys cAdvisor as a Docker container for per-container CPU/memory metrics, scraped by Prometheus on citadel. Guards against the loki-driver container-recreate deadlock on warrig/citadel. See `roles/cadvisor/README.md`. |
| `deploy_promtail.yml` | `[promtail]` (Pis) | Deploys Promtail as a Docker container shipping container logs plus host syslog/auth/kernel/dpkg logs to Loki on citadel. See `roles/promtail/README.md`. |
| `deploy_ntfy_server.yml` | black-pearl | Deploys ntfy as a Docker container. Every alert in the lab (borgmatic, rsnapshot, dsm_config_audit, and the rest) flows through it. See `roles/ntfy_server/README.md`. |
| `configure_ntfy_client.yml` | all hosts | Renders `/etc/ntfy/client.yml` so every host's `ntfy` CLI points at the self-hosted server instead of silently defaulting to the public `ntfy.sh`. See `roles/ntfy_client/README.md`. |
| `configure_borgmatic_warrig.yml` | warrig + gastown | warrig's counterpart to `configure_borgmatic_citadel.yml`: installs borgmatic, renders config + hook scripts (adopting a previously hand-managed `/etc/borgmatic/config.yaml`), generates an SSH key, authorizes it on gastown, and schedules the daily 04:00 push. First run must be `--check --diff`. See `roles/borgmatic/README.md`. |
| `configure_dsm_config_audit.yml` | gastown (Synology) | Drops a read-only DSM config-drift export script in `nux`'s home and pins the Windmill runner's SSH key to it with a forced `command=`, so a nightly Windmill flow can snapshot config-drift surfaces. See `roles/dsm_config_audit/README.md`. |
| `update_docker_images.yml` | warrig + citadel + the three Pis (any subset via `-e docker_update_target=`) | **DESTRUCTIVE.** One-shot `watchtower --run-once` to pull newer digests for every running container's current tag and recreate what moved, then `docker system prune -af`. Runs unescalated as the `docker`-group user; no become key needed. gastown is excluded and must stay excluded (no `docker` on PATH, no socket). Read the header comment before running it: 71 of the 90 containers in scope are loki-logged, and Semaphore itself is one of them. |
| `upgrade_raspberrypi.yml` | Raspberry Pis | Serial Debian point-release upgrade: backs up state, rewrites apt sources to the next release, runs the upgrade, makes sure networking comes back, reboots, and verifies. Driven by a `release_map` in `roles/dist_upgrade/vars/`. |
| `configure_pwnbox.yml` | localhost | PwnBox bootstrap: installs the `package_list` from `common`, then clones dotfiles, stows them, and installs Starship + Atuin. |
| `pull/local.yml` | Raspberry Pis (via `ansible-pull`) | ansible-pull entrypoint importing the six Pi-relevant playbooks (node_exporter, cadvisor, docker log rotation, ufw, ntfy_client, backup_targets) in one shot. See "Pull mode (future)" below. |

## Roles

One role per playbook, described in the Playbooks table above. The complex
ones (`borgmatic`, `rsnapshot`, `dsm_config_audit`, `cadvisor`,
`promtail`, `ntfy_server`, `firewalld`, `docker_log_rotation`) carry their own
README. Every role's tunables live in its commented `defaults/main.yml`. That
file is the variable reference, deliberately not duplicated here.

## Inventory

One canonical inventory: `inventory/hosts` (the `ansible.cfg` default, so no
`-i` needed). Base groups are `[raspberrypis]`, `[archlinux]` (citadel +
warrig), `[synology]` (gastown), and `[pwnbox]`; the role/playbook target
groups (`node_exporter`, `cadvisor`, `promtail`, `ntfy_server`, `firewalld`,
`backup_targets`, `borgmatic_source`) are built from those via `:children`.
Per-host connection details (IPs, become, gastown's SynoCommunity Python 3.12
interpreter pin) live in `inventory/host_vars/<name>.yml`; secrets in
`inventory/group_vars/*/vault.yml` (ansible-vault).

Blast radius comes from each playbook's `hosts:` pattern. Add `--limit` to
narrow it further.

## Running

Semaphore is the runner. It clones this repo from GitHub on `main`, and most
playbooks have a template, so a run is a button press and the output lands in
the task log.

Two playbooks deliberately have **no** template, and should not be given one:

- `configure_pwnbox.yml`. `pwnbox` is `ansible_connection=local`, so a
  Semaphore run targets the Semaphore container itself, and the role deletes
  `~/.bashrc` and `~/.profile` before stowing dotfiles over the top. The same
  trap applies to running it from a shell on warrig, where it targets warrig.
- `upgrade_raspberrypi.yml`. A fleet-wide distro upgrade wants a human
  watching for prompts, not a button. Run it from a shell.

`update_docker_images.yml` does have one, marked DESTRUCTIVE, with an
acknowledgement box and the caveat that the box is friction rather
than enforcement. It is the one template whose blast radius is decided by
something other than this repo: Watchtower picks what to recreate, so what a
run does depends on what registries have published since the last one. Its
`hosts:` default covers all five docker hosts; narrow it with
`--extra-vars docker_update_target=` exactly as with the log-rotation
templates, not with a Limit field.

`configure_docker_log_rotation.yml` needs one template per target, because its
`hosts:` is `{{ docker_daemon_target | default('raspberrypis') }}` and
`--limit` can only narrow a pattern, never widen it. There are three: the Pi
default, plus warrig and citadel passing `--extra-vars docker_daemon_target=`.

Three consequences of Semaphore owning execution:

- It runs pushed state. A local commit changes nothing until it reaches
  `main` on GitHub.
- Never put `ansible_ssh_private_key_file` in inventory. Semaphore
  authenticates with an ssh-agent holding its own key, and an inventory
  variable overrides that with a path that does not exist in the container.
- Vault comes from `ANSIBLE_VAULT_PASSWORD_FILE`, set in the Semaphore
  environment. `.vault_pass` is gitignored, so it is never in the clone.

citadel and warrig have no passwordless sudo, so any play with `become` needs
a Become key on the Semaphore inventory. The Pis and gastown do not need it
(gastown sets `ansible_become: false`).

**That key now exists.** Checked against the API on 2026-08-18: inventory
`lab-hosts` carries `become_key_id: 38`, the `lab sudo (warrig + citadel)`
`login_password` credential. This paragraph previously said the key was
missing and was the one thing blocking Semaphore; that was true when written
and is not any more. The symptom it describes, a run that succeeds on the Pis
and fails on the two Arch hosts with `Missing sudo password`, is what to look
for if the key is ever unset again.

`update_docker_images.yml` never needed it either way: it runs unescalated as
the `docker`-group user. Note that `become: false` at play level would NOT
have achieved that, because an inventory `ansible_become` outranks the play
keyword; it sets `ansible_become: false` as a play var instead, which does
outrank host_vars.

Before that key was added these templates failed on citadel and warrig: node_exporter,
firewalld, backup_targets, ntfy_client, both borgmatic templates, cAdvisor
(warrig only), and the two per-host docker log rotation ones. This predates
warrig gaining `ansible_become: true`; citadel was already affected. The
warrig change widens which plays attempt escalation, it did not create the
gap.

Running from a shell still works, and is the better choice for a first apply
or anything you want to watch closely:

```sh
ansible-playbook playbooks/<playbook>.yml
# add --check --diff for a dry run
```

The PwnBox is the exception. It bootstraps via `ansible-pull` so a fresh HTB
box can configure itself:

```sh
# -l pwnbox: the host is named "pwnbox" (not 127.0.0.1), so ansible-pull's
# implicit hostname/localhost limit won't match it on an HTB box.
ansible-pull -U https://github.com/cam-barts/ansible.git \
    -d ~/ansible-pwnbox -i inventory/ -l pwnbox -C main \
    playbooks/configure_pwnbox.yml
```

## Pull mode (not usable yet)

`pull/local.yml` is the `ansible-pull` entrypoint for the Raspberry Pi fleet.
It imports the six Pi-relevant playbooks (node_exporter, cadvisor, docker log
rotation, ufw, ntfy_client, backup_targets) so one pull run configures a Pi
end to end. It passes `--syntax-check`, and every playbook it imports is
applied regularly in push mode, so the content is sound:

```sh
ansible-pull -U https://github.com/cam-barts/ansible.git -C stable \
    -i inventory/ pull/local.yml
```

That command does not work today. Checked 2026-08-18, three things are missing,
and only the last is the scheduling question this section used to describe:

1. **`ansible-pull` is not installed on any Pi.** `wellerman`, `black-pearl`
   and `billy-of-tea` have `git` but no ansible at all. Pull mode needs ansible
   present on each target, which is the one prerequisite nothing in this repo
   currently installs.
2. **The `stable` branch is not on the remote.** It exists locally and is
   currently identical to `main`, but `git ls-remote --heads origin` lists only
   `main`, so `-C stable` fails at clone. Push it, or drop the `-C`.
3. **No timer.** The intended rollout is a canary systemd timer on
   `billy-of-tea` first, then the rest of the fleet.

Worth being deliberate about step 3 rather than treating it as a formality: a
timer means the Pis apply config unattended, so a bad commit on `stable`
reaches hardware with nobody watching. That is the reason for the canary and
for `stable` existing separately from `main` in the first place.

Note also that `pirate`'s login shell on the Pis is `fish`. Ansible forces
`/bin/sh` for module execution so push mode is unaffected, but any hand-written
pull wrapper or timer `ExecStart` should not assume POSIX shell syntax works
interactively there.

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

- `wellerman`, `black-pearl`, `billy-of-tea`. Raspbian on ARM. Run Docker
  workloads, backed up to gastown via rsnapshot.
- `citadel`. EndeavourOS server, reports `os_family: Archlinux`. Runs the
  heavier docker stack, backed up via borgmatic to gastown.
- `warrig`. EndeavourOS workstation, also `os_family: Archlinux`. Backup
  source for gastown's rsnapshot tier, and pushes its own daily borgmatic
  backup to gastown. Hosts Semaphore.
- `gastown`. Synology DSM 7.x. Backup destination for both the rsnapshot tier
  (pull) and the borgmatic tier (push from citadel and warrig).
- `pwnbox`. HackTheBox Parrot VM. Configured via `ansible-pull`.

## Upstream and credits

Built on [Ansible](https://github.com/ansible/ansible) (GPL-3.0) and three
collections: [community.general](https://github.com/ansible-collections/community.general)
(GPL-3.0), [ansible.posix](https://github.com/ansible-collections/ansible.posix)
(GPL-3.0), and [community.docker](https://github.com/ansible-collections/community.docker)
(GPL-3.0-or-later and Apache-2.0).

The roles automate third-party software rather than vendoring any of it. Each
role README credits its own upstream; collected here:

| Software | Licence | Used by |
|---|---|---|
| [borgmatic](https://torsion.org/borgmatic/) | GPL-3.0-or-later | `borgmatic` |
| [BorgBackup](https://www.borgbackup.org/) | BSD-3-Clause | `borgmatic` |
| [cAdvisor](https://github.com/google/cadvisor) | Apache-2.0 | `cadvisor` |
| [Docker Engine](https://github.com/moby/moby) | Apache-2.0 | `docker_log_rotation` |
| [firewalld](https://firewalld.org/) | GPL-2.0-or-later | `firewalld` |
| [ntfy](https://github.com/binwiederhier/ntfy) | Apache-2.0 or GPL-2.0 | `ntfy_server`, `ntfy_client` |
| [Prometheus node_exporter](https://github.com/prometheus/node_exporter) | Apache-2.0 | `node_exporter` |
| [Promtail, part of Grafana Loki](https://github.com/grafana/loki) | AGPL-3.0 | `promtail` |
| [rsnapshot](https://rsnapshot.org/) | GPL-2.0 | `rsnapshot` |
| [ufw](https://launchpad.net/ufw) | GPL-3.0 | `ufw` |

Licences were read from the upstream repositories and distro package metadata
on 2026-08-17. Synology DSM's own utilities (`synoschedtask`, `synopkg`,
`synofirewall`, `synoschedule`) are proprietary and vendor-supplied; the
`rsnapshot` and `dsm_config_audit` roles call them but ship none of
their code.
