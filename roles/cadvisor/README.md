# cadvisor

Deploys [cAdvisor](https://github.com/google/cadvisor) as a Docker container so
Prometheus can collect per-container CPU/memory metrics from the host.

Companion to the `node_exporter` role: node_exporter covers host-level metrics,
cAdvisor covers per-container metrics. Prometheus (on citadel) scrapes each
cAdvisor at `<host>:{{ cadvisor_port }}`. Add the target to
`~/docker/monitoring/prometheus.yml` on citadel and reload (`docker kill
--signal=HUP prometheus`).

## Requirements

- Docker running on the target, with the login user (`pirate` or `nux`) in the
  `docker` group so the `docker_*` tasks run without `become`.
- Only the Docker-SDK install step uses `become` (apt/pacman), so run with
  `--ask-become-pass`. Enabling `cadvisor_boot_fix` adds two more `become`
  tasks (unit file and `systemctl enable`), so that flag also needs
  `--ask-become-pass` on password-gated hosts (warrig, citadel).

## Usage

```bash
ansible-playbook playbooks/deploy_cadvisor.yml --ask-become-pass
```

Any host in the `cadvisor` inventory group is covered, and future Pis added to
`[raspberrypis]` are picked up automatically.

Tunables are documented inline in `defaults/main.yml`.

## The loki recreate hazard

`docker_container` has no in-place update for most settings: any difference
between the desired spec and the live container means stop, rm, create. On
warrig and citadel dockerd's default log driver is `loki`, and tearing down a
container under that driver deadlocks. The play does not fail, it hangs, and so
does anything else touching that container afterwards.

So before it touches anything, the role reads the daemon's default log driver
and, on `loki` hosts, dry-runs the exact same container spec in check mode. If
that dry run reports differences, the role fails with the list of differing
settings instead of hanging. This is deliberately slightly over-broad, since a
network attach or detach is reported as a difference without actually being a
recreate, because a spurious refusal costs a re-run and a spurious recreate
costs a wedged daemon.

Two ways out of a refusal:

1. Preferred: set host_vars so the spec matches what is already running
   (recipes below). The refusal message lists exactly what differs.
2. `cadvisor_allow_recreate: true` skips the check entirely. Only for when you
   genuinely want the container rebuilt, an image bump for instance, and are
   ready to unwedge dockerd if it hangs.

Hosts using `json-file` (the Pis) never hit any of this; the dry run is skipped.

## Network mode, and why warrig differs

A Docker published port is reached through Docker's own iptables chains, which
are consulted before firewalld. The host firewall therefore cannot scope it.
Measured on warrig 2026-08-17, from black-pearl, which no rule permits:

```
warrig:8080  OPEN     warrig:9100  BLOCKED     warrig:22  OPEN
```

9100 is node_exporter, an ordinary host service, and firewalld scoped it to
citadel correctly. 8080 was cAdvisor's published port, open to the whole LAN.

So warrig runs cAdvisor with `cadvisor_network_mode: host` instead. In host
mode cAdvisor binds `cadvisor_port` on the host directly, becomes an ordinary
listener on the INPUT path, and the rich rule in `roles/firewalld` applies to
it exactly as it does to 9100. Setting the mode drops `published_ports`,
`default_host_ip` and `cadvisor_networks` from the spec, and passes `--port`
explicitly so `cadvisor_port` stays authoritative.

The Pis keep the published port. They are on `json-file`, their exposure is
the same either way, and changing them would buy nothing.

## Host recipes

`host_vars/warrig.yml`, in `[cadvisor]`, host networking so firewalld governs
8080, 512m cap, no `--docker_only`:

```yaml
cadvisor_memory: "512m"
cadvisor_docker_only: false
cadvisor_network_mode: host
cadvisor_publish_port: false
cadvisor_boot_fix: true
```

`host_vars/citadel.yml`, deliberately NOT in the group. Its cAdvisor has no
host port at all: Prometheus shares `monitoring_default` and scrapes it by
container name, so there is nothing for a firewall to scope. If it is ever
folded in, these are the values read off the live container:

```yaml
cadvisor_memory: "512m"
cadvisor_docker_only: false
cadvisor_publish_port: false
cadvisor_networks:
  - name: monitoring_default
```

Both run without `--docker_only=true`, so they report every system cgroup, not
just docker's. Turning it on is a command change, which is a recreate, which is
the hazard above. Leave it for a maintenance window.

## Cutting warrig over to host networking

This is a recreate on a loki host, so it is the hazard above and needs doing
deliberately, once. Order matters.

1. Apply the firewall rule first. It is inert while 8080 is still a published
   port, so this is safe to do ahead of time, and doing it after would leave
   Prometheus unable to reach warrig in the gap.

   ```bash
   ansible-playbook playbooks/configure_firewalld.yml --limit warrig --ask-become-pass
   ```

2. Stop the container by hand before letting Ansible near it. A clean stop lets
   the loki driver flush; it is removing a *running* container that deadlocks.

   ```bash
   docker stop cadvisor
   ```

3. Cut over, lifting the guard for this run only. Do not put
   `cadvisor_allow_recreate` in host_vars, or the guard is disarmed forever.

   ```bash
   ansible-playbook playbooks/deploy_cadvisor.yml --limit warrig \
       --ask-become-pass -e cadvisor_allow_recreate=true
   ```

4. Verify, from three angles:

   ```bash
   docker inspect cadvisor --format '{{ .HostConfig.NetworkMode }}'   # host
   ssh black-pearl 'timeout 5 bash -c "</dev/tcp/192.168.1.168/8080"' # should fail
   ssh citadel     'timeout 5 bash -c "</dev/tcp/192.168.1.168/8080"' # should succeed
   ```

   Then confirm the Prometheus target recovered:
   `curl -s localhost:9090/api/v1/targets` on citadel, cadvisor job, instance
   warrig, health `up`.

A check run before the cutover should refuse with exactly two differences,
`network_mode` and `command`, and nothing else. More than that means something
drifted and is worth reading before proceeding.

This run also adopts `cadvisor-boot-fix.service`. warrig already has a
hand-written copy enabled; the role's template differs from it only in
comments and the `ansible_managed` header, so the unit's behaviour does not
change.

## cAdvisor's boot race (`cadvisor_boot_fix`)

cAdvisor v0.52.1 probes dockerd's `/info` exactly once at startup, with a
hardcoded 10s timeout, and never retries. On a host where dockerd is restoring
dozens of containers at boot, cAdvisor is itself one of them, asks too early,
times out, and then runs permanently without the Docker factory. It publishes
cgroup-path-only metrics with no `name` label while still passing its own
liveness-only healthcheck. There is no flag to extend the timeout.

`cadvisor_boot_fix: true` installs `cadvisor-boot-fix.service`, which waits for
`docker info` to answer, waits for the container count to stop growing, then
restarts cAdvisor once. Modelled on the hand-written unit that has been running
on warrig.

The role only enables the unit, it never starts it. Starting it would
`docker restart cadvisor`, which is a teardown, which on a loki host is the
deadlock above. It takes effect at the next boot. Needs `--ask-become-pass`.

## Upstream

Deploys [cAdvisor](https://github.com/google/cadvisor) (Apache-2.0), from the
`gcr.io/cadvisor/cadvisor` image. Metrics are consumed by
[Prometheus](https://github.com/prometheus/prometheus) (Apache-2.0). This role
only manages the container and its configuration.
