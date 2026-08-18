# ntfy_server

**Every alert in the lab flows through this server** — borgmatic, rsnapshot,
dsm_config_audit, and anything else using the `ntfy_client` role all publish
here. Treat applying this role with care: a container recreate is a brief
alert-delivery outage (seconds), not data loss — black-pearl runs the
`json-file` docker logging driver, same as the rest of the Pi fleet, so
there's no teardown deadlock risk (contrast `roles/cadvisor/README.md`'s
loki hazard on warrig/citadel) — but it's still the one host where "brief
outage" means "you might miss a real alert while it's down."

Deploys [ntfy](https://ntfy.sh/) as a Docker container on black-pearl only.
Companion to `ntfy_client`, which configures every other host's ntfy CLI
default-host to point at this server.

## Requirements

- Docker running on black-pearl, with `pirate` in the `docker` group so the
  `docker_*` tasks run without `become`.
- Only the Docker-SDK install step uses `become` (apt) — run with
  `--ask-become-pass`. Harmless: black-pearl's host_vars already set
  `ansible_become: true`.

## Secrets

The live container sets `NTFY_WEB_PUSH_PRIVATE_KEY`, the VAPID private key
for browser background push. That's a real secret and is **not** committed
to this repo. `defaults/main.yml` points
`ntfy_server_web_push_private_key` at `vault_ntfy_server_web_push_private_key`
— same indirection pattern as `inventory/group_vars/synology`. Before first
apply, create the vault file:

```bash
ansible-vault create inventory/group_vars/ntfy_server/vault.yml
```

and set:

```yaml
vault_ntfy_server_web_push_private_key: <the live NTFY_WEB_PUSH_PRIVATE_KEY value>
```

Read the live value off black-pearl's `docker-compose.yml`
(`/home/pirate/ntfy/docker-compose.yml`, the `NTFY_WEB_PUSH_PRIVATE_KEY` env
var) and paste it straight into the vault — never into git in plaintext.
The matching public key is not secret (it's handed to every subscribing
browser by the Web Push protocol) and lives directly in `defaults/main.yml`.

## Usage

```bash
ansible-playbook playbooks/deploy_ntfy_server.yml --ask-become-pass
```

Targets the `ntfy_server` inventory group (black-pearl only, in
`inventory/hosts`). The old `/home/pirate/ntfy/docker-compose.yml` is
commented out in place (2026-08-17) with a "managed by Ansible" header —
reference only, so a habitual `docker compose up` can't collide with the
ansible-managed container.

Tunables are documented inline in `defaults/main.yml`.

## Source of truth

Read live 2026-08-17 from black-pearl: `/home/pirate/ntfy/docker-compose.yml`,
`data/server.yml`, and `docker inspect ntfy`.

- Container spec (`vars/main.yml`) mirrors the live container: same image tag
  (already pinned at `v2.11.0` — no change needed), same env, same
  `user: 1000:1000`, same volumes, same published port (`2867:80`), same
  network (`ntfy_default`, the compose-project network — used by name so
  this role attaches to it rather than creating a duplicate default bridge).
- `data/server.yml` on the live host is ntfy's full ~380-line shipped
  template; only six directives are actually active (everything else is
  commented-out documentation). `templates/server.yml.j2` reproduces just
  those six — `base-url`, `behind-proxy`, `attachment-cache-dir`,
  `attachment-file-size-limit`, `message-size-limit`,
  `visitor-attachment-total-size-limit` — rather than carrying ntfy's own
  docs as dead weight in this repo. See https://ntfy.sh/docs/config/ for the
  full option reference if more settings need enabling later.
- `data/` also holds `server.yml.bak.20260526-183726`, a stale manual
  backup — left alone, this role only manages `server.yml` itself.
