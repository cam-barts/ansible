# ntfy_server

Every alert in the lab flows through this server. Borgmatic, rsnapshot,
dsm_config_audit, and anything else using the `ntfy_client` role all publish
here, so apply this role with care. A container recreate is a brief
alert-delivery outage of a few seconds rather than data loss, and black-pearl
runs the `json-file` docker logging driver like the rest of the Pi fleet, so
there is no teardown deadlock risk (contrast the loki hazard on warrig and
citadel in `roles/cadvisor/README.md`). It is still the one host where "brief
outage" means you might miss a real alert while it is down.

Deploys [ntfy](https://ntfy.sh/) as a Docker container on black-pearl only.
Companion to `ntfy_client`, which configures every other host's ntfy CLI
default-host to point at this server.

## Requirements

- Docker running on black-pearl, with `pirate` in the `docker` group so the
  `docker_*` tasks run without `become`.
- Only the Docker-SDK install step uses `become` (apt), so run with
  `--ask-become-pass`. That is harmless here: black-pearl's host_vars already
  set `ansible_become: true`.

## Secrets

The live container sets `NTFY_WEB_PUSH_PRIVATE_KEY`, the VAPID private key
for browser background push. That is a real secret and is not committed to
this repo in plaintext. `defaults/main.yml` points
`ntfy_server_web_push_private_key` at `vault_ntfy_server_web_push_private_key`,
the same indirection pattern as `inventory/group_vars/synology`.

`inventory/group_vars/ntfy_server/vault.yml` now exists, created 2026-08-18 by
reading the value off the running container on black-pearl and encrypting it
straight into place. Until then this playbook could not run at all: it aborted
on `'vault_ntfy_server_web_push_private_key' is undefined` before reaching the
container task, so the role had never been applied.

The captured value is confirmed correct by the apply itself. `Run ntfy
container` reports `ok` rather than `changed`, which means the rendered
container spec, env vars included, matches the live container exactly. A wrong
key would have shown up as a diff and a recreate.

To rotate or recreate it:

```bash
ansible-vault edit inventory/group_vars/ntfy_server/vault.yml
```

The matching public key is not secret, since Web Push hands it to every
subscribing browser, and it lives directly in `defaults/main.yml`. The two are
a pair: rotating the private key means rotating the public one and
re-subscribing every browser client.

## Usage

```bash
ansible-playbook playbooks/deploy_ntfy_server.yml --ask-become-pass
```

Targets the `ntfy_server` inventory group (black-pearl only, in
`inventory/hosts`). The old `/home/pirate/ntfy/docker-compose.yml` is
commented out in place (2026-08-17) with a "managed by Ansible" header, so it
is reference only and a habitual `docker compose up` can't collide with the
ansible-managed container.

Tunables are documented inline in `defaults/main.yml`.

## Source of truth

Read live 2026-08-17 from black-pearl: `/home/pirate/ntfy/docker-compose.yml`,
`data/server.yml`, and `docker inspect ntfy`.

- Container spec (`vars/main.yml`) mirrors the live container: same image tag
  (already pinned at `v2.11.0`, no change needed), same env, same
  `user: 1000:1000`, same volumes, same published port (`2867:80`), same
  network (`ntfy_default`, the compose-project network, used by name so this
  role attaches to it rather than creating a duplicate default bridge).
- `data/server.yml` on the live host is ntfy's full ~380-line shipped
  template, and only six directives are actually active (everything else is
  commented-out documentation). `templates/server.yml.j2` reproduces just
  those six (`base-url`, `behind-proxy`, `attachment-cache-dir`,
  `attachment-file-size-limit`, `message-size-limit`,
  `visitor-attachment-total-size-limit`) rather than carrying ntfy's own docs
  as dead weight in this repo. See https://ntfy.sh/docs/config/ for the full
  option reference if more settings need enabling later.
- `data/` also holds `server.yml.bak.20260526-183726`, a stale manual backup.
  Left alone; this role only manages `server.yml` itself.

## Upstream

Deploys [ntfy](https://github.com/binwiederhier/ntfy), dual-licensed
Apache-2.0 or GPL-2.0, from the `binwiederhier/ntfy` image. This role manages
the container and a trimmed `server.yml`; the full option reference stays
upstream at https://ntfy.sh/docs/config/.
