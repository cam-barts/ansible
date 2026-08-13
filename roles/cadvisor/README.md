# cadvisor

Deploys [cAdvisor](https://github.com/google/cadvisor) as a Docker container so
Prometheus can collect per-container CPU/memory metrics from the host.

Companion to the `node_exporter` role: node_exporter covers host-level metrics,
cAdvisor covers per-container metrics. Prometheus (on citadel) scrapes each
cAdvisor at `<host>:{{ cadvisor_port }}` — add the target to
`~/docker/monitoring/prometheus.yml` on citadel and reload (`docker kill
--signal=HUP prometheus`).

## Requirements

- Docker running on the target, with the login user (e.g. `pirate`, `nux`) in
  the `docker` group so the `docker_*` tasks run without `become`.
- Only the Docker-SDK install step uses `become` (apt/pacman) — run with
  `--ask-become-pass`.

## Usage

```bash
ansible-playbook -i inventory/node_exporter playbooks/deploy_cadvisor.yml --ask-become-pass
```

Any host in the `cadvisor` inventory group is covered — future Pis added to
`[raspberrypis]` are picked up automatically.

## Key variables (see `defaults/main.yml`)

| var | default | note |
|-----|---------|------|
| `cadvisor_version` | `v0.52.1` | image tag (multi-arch: amd64/arm64/armv7) |
| `cadvisor_memory` | `256m` | cap; bump to `512m` on larger hosts |
| `cadvisor_publish_port` / `cadvisor_port` | `true` / `8080` | publish for LAN scrape; set `false` on citadel |
| `cadvisor_docker_only` | `true` | limit to docker cgroups — lighter on weak CPUs |
| `cadvisor_networks` | `[]` | attach extra networks, e.g. `[{name: monitoring_default}]` on citadel |

### citadel note

citadel's cAdvisor sits on the `monitoring_default` network and is scraped by
container name, not a host port. To manage it here, set in host_vars:

```yaml
cadvisor_publish_port: false
cadvisor_networks:
  - name: monitoring_default
```
