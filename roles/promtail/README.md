# promtail

Deploys [Promtail](https://grafana.com/docs/loki/latest/send-data/promtail/)
as a Docker container on the Raspberry Pi fleet so Grafana Loki (on citadel)
gets Docker container logs and host syslog/auth/kernel/dmesg/dpkg logs.

Companion to `cadvisor` (per-container metrics) and `node_exporter` (host
metrics) — this role covers logs. `network_mode: host` so Promtail reaches
Loki without Docker network overhead and picks up the real hostname.

## Requirements

- Docker running on the target, with the login user (`pirate`) in the
  `docker` group so the `docker_*` tasks run without `become`.
- Only the Docker-SDK install step uses `become` (apt) — run with
  `--ask-become-pass`. Harmless: the Pis' host_vars already set
  `ansible_become: true`.

## Usage

```bash
ansible-playbook playbooks/deploy_promtail.yml --ask-become-pass
```

Any host in the `promtail` inventory group is covered — future Pis added to
`[raspberrypis]` are picked up automatically (`[promtail:children]
raspberrypis` in `inventory/hosts`).

Tunables are documented inline in `defaults/main.yml`.

## Source of truth

Read live 2026-08-17 from all three Pis (`wellerman`, `black-pearl`,
`billy-of-tea`): `/home/pirate/promtail/docker-compose.yml` and
`promtail-config.yml`, plus `docker inspect promtail` on each. Findings:

- **`promtail-config.yml` is byte-identical on all three Pis** — no per-host
  differences to parameterize. The file is fully driven by
  `-config.expand-env=true` substituting `${HOSTNAME}` / `${LOKI_URL}` at
  container start, so one template (`templates/promtail-config.yml.j2`)
  covers the fleet.
- The only per-host difference in `docker-compose.yml` is the `HOSTNAME` env
  var (set to each Pi's own name) — handled here via
  `env.HOSTNAME: "{{ inventory_hostname }}"`.
- Container spec (`vars/main.yml`) mirrors `docker inspect promtail` on all
  three exactly: same mounts, `network_mode: host`, `restart_policy:
  unless-stopped`, same command, same `extra_hosts` pin for
  `citadel.shadeking.cam.local`, same named volume
  (`promtail_promtail-positions`, i.e. the compose-project-prefixed name —
  used verbatim so this role attaches to the volume compose already created
  instead of creating a second one).
- The old `docker-compose.yml` on each Pi is commented out in place
  (2026-08-17) with a "managed by Ansible" header — kept for reference, but
  inert so a habitual `docker compose up` can't collide with the
  ansible-managed container. This role owns the container directly via
  `community.docker.docker_container`, same as `cadvisor`.

## Image pin

The live containers ran `grafana/promtail:latest`. Resolved via the Docker
Hub registry API on 2026-08-17: `latest` and `3.6.8` both resolve to the
identical manifest-list digest
(`sha256:6cfa64ec432b24a912d640e2edb940eeae2666f61861a66c121d763dd7241381`) —
confirmed by pulling each tag's `Docker-Content-Digest` header directly from
`registry-1.docker.io`, and by `docker exec promtail promtail --version`
inside the running container reporting `3.6.8`. Pinning to `3.6.8` means:

- **First apply recreates the container once.** `docker_container` treats
  the tag string itself as part of the spec, so `grafana/promtail:latest` →
  `grafana/promtail:3.6.8` is a config difference even though the image
  content is identical — same digest, same layers, brief restart only.
- This is safe here: the Pis default to the `json-file` docker logging
  driver, not `loki` — no risk of the teardown deadlock documented in
  `roles/cadvisor/README.md`. No recreate guard is needed or included.
- After that one recreate, future runs are a no-op until `promtail_version`
  is bumped by hand — Promtail's own `latest` tag will keep moving upstream,
  but this role no longer follows it silently.
