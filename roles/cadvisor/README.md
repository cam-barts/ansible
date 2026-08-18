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
  `--ask-become-pass`. Enabling `cadvisor_boot_fix` adds two more `become`
  tasks (unit file + `systemctl enable`), so that flag also needs
  `--ask-become-pass` on password-gated hosts (warrig, citadel).

## Usage

```bash
ansible-playbook playbooks/deploy_cadvisor.yml --ask-become-pass
```

Any host in the `cadvisor` inventory group is covered — future Pis added to
`[raspberrypis]` are picked up automatically.

Tunables are documented inline in `defaults/main.yml`.

## The loki recreate hazard

`docker_container` has no in-place update for most settings: any difference
between the desired spec and the live container means stop → rm → create. On
warrig and citadel dockerd's default log driver is `loki`, and tearing down a
container under that driver deadlocks — the play does not fail, it *hangs*, and
so does anything else touching that container afterwards.

So before it touches anything, the role reads the daemon's default log driver
and, on `loki` hosts, dry-runs the exact same container spec in check mode. If
that dry run reports differences, the role fails with the list of differing
settings instead of hanging. This is deliberately slightly over-broad — a
network attach/detach is reported as a difference without actually being a
recreate — because a spurious refusal costs a re-run and a spurious recreate
costs a wedged daemon.

Two ways out of a refusal:

1. **Preferred:** set host_vars so the spec matches what is already running
   (recipes below). The refusal message lists exactly what differs.
2. `cadvisor_allow_recreate: true` skips the check entirely. Only for when you
   genuinely want the container rebuilt — an image bump, say — and are ready to
   unwedge dockerd if it hangs.

Hosts using `json-file` (the Pis) never hit any of this; the dry run is skipped.

## Folding warrig and citadel in

Neither is in `[cadvisor:children]` today. To manage them here, add the host to
the group and set the host_vars below — they were read off the live containers,
so the first apply is a no-op and no recreate is attempted.

`host_vars/warrig.yml` — published port, but created by hand (empty host IP),
512m cap, no `--docker_only`:

```yaml
cadvisor_memory: "512m"
cadvisor_docker_only: false
cadvisor_publish_host_ip: ""
```

`host_vars/citadel.yml` — no published port, scraped by container name over
`monitoring_default`, same 512m cap and no `--docker_only`:

```yaml
cadvisor_memory: "512m"
cadvisor_docker_only: false
cadvisor_publish_port: false
cadvisor_networks:
  - name: monitoring_default
```

Note both run without `--docker_only=true`, so they report every system cgroup,
not just docker's. Turning it on is a command change, which is a recreate,
which is the hazard above — leave it for a maintenance window.

## cAdvisor's boot race (`cadvisor_boot_fix`)

cAdvisor v0.52.1 probes dockerd's `/info` exactly once at startup, with a
hardcoded 10s timeout, and never retries. On a host where dockerd is restoring
dozens of containers at boot, cAdvisor — itself one of them — asks too early,
times out, and then runs permanently without the Docker factory: it publishes
cgroup-path-only metrics with no `name` label, while still passing its own
liveness-only healthcheck. There is no flag to extend the timeout.

`cadvisor_boot_fix: true` installs `cadvisor-boot-fix.service`, which waits for
`docker info` to answer, waits for the container count to stop growing, then
restarts cAdvisor once. Modelled on the hand-written unit that has been running
on warrig.

The role only **enables** the unit, it never starts it: starting it would
`docker restart cadvisor`, which is a teardown, which on a loki host is the
deadlock above. It takes effect at the next boot. Needs `--ask-become-pass`.
