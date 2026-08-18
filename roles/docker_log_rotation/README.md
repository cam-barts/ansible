# docker_log_rotation

Manages the whole of `/etc/docker/daemon.json`, not just the log stanza.

The name is a leftover from when it only set `log-driver: json-file` with a
10m×3 cap. It is kept on purpose: renaming to `docker_daemon` would churn
`playbooks/configure_docker_log_rotation.yml`, the top-level README and any
inventory references, in exchange for nothing. Read "log rotation" as the
reason the role was born, not the limit of what it does.

## How it works

`docker_daemon_config` is merged (shallow `combine`) over whatever is already
on disk, then written with `to_nice_json`. Keys you don't mention survive.

The default value reproduces the old behaviour exactly, so the Pis are
unaffected:

```yaml
docker_daemon_config:
  log-driver: json-file
  log-opts:
    max-size: "10m"
    max-file: "3"
```

### First-apply diffs are expected

`to_nice_json` sorts keys and indents with 4 spaces. A host whose live
`daemon.json` was hand-written in another style shows a formatting-only diff on
first apply even though the JSON content is identical. Read the diff, confirm
nothing semantic moved, then apply. Observed 2026-08-17:

- warrig: the live file is already 4-space and key-sorted (it looks to have
  been written by this role at some point) and has no trailing newline. Expect
  a one-byte diff.
- citadel: the live file is 2-space and unsorted. Expect a whole-file reformat
  with no content change.

Applied 2026-08-18, and the prediction held. Both hosts reported exactly one
changed task and the handler skipped, as designed. Verified afterwards that it
was cosmetic on both, three ways: the on-disk JSON now parses equal to the
`docker_daemon_config` in host_vars, no unmanaged keys were dropped, and
`docker info` still agrees with the file on `LoggingDriver` and
`DockerRootDir`. So there is no pending behaviour change waiting on a restart,
and the "dockerd needs a manual restart" warning, while correct that the write
happened, had nothing semantic behind it in this instance.

That distinction is the thing to check on any future `changed` here. The
warning fires on any write at all, so it cannot by itself tell you whether a
restart would change how dockerd behaves. Compare the parsed file against
host_vars and against `docker info` before deciding to restart a loki host.

## Per-host config

The verbatim live configs below are transcribed from each host on 2026-08-17.
Drop them into `inventory/host_vars/<host>.yml` (that directory is not this
role's to edit) and the first apply is a no-op modulo formatting.

`inventory/host_vars/warrig.yml`:

```yaml
docker_daemon_config:
  data-root: /home/docker
  debug: true
  features:
    buildkit: true
  log-driver: loki
  log-opts:
    keep-file: "true"
    loki-batch-size: "400"
    loki-max-backoff: 800ms
    loki-retries: "2"
    loki-timeout: 1s
    loki-url: http://citadel.shadeking.cam.local:3100/loki/api/v1/push
    mode: non-blocking
  runtimes:
    nvidia:
      args: []
      path: nvidia-container-runtime
```

`inventory/host_vars/citadel.yml`:

```yaml
docker_daemon_config:
  debug: true
  log-driver: loki
  log-opts:
    keep-file: "true"
    loki-batch-size: "5000"
    loki-max-backoff: 800ms
    loki-retries: "2"
    loki-timeout: 3s
    loki-url: http://citadel.shadeking.cam.local:3100/loki/api/v1/push
    mode: non-blocking
```

Note the deliberate divergence: warrig batches 400 with a 1s timeout, citadel
batches 5000 with 3s. citadel is the Loki host, warrig ships over the network.
Keep the values as-is unless there's a reason.

## The restart handler is off by default

`docker_daemon_allow_restart: false`.

Restarting dockerd on a loki-logging host risks a deadlock: the log driver
ships to a Loki container supervised by the daemon being restarted, so
containers that cannot flush can wedge the restart. Both warrig and citadel are
in that category.

With the flag false the `Restart docker` handler no-ops and the role prints a
warning that a manual restart is owed. Pick your moment, then:

```sh
sudo systemctl restart docker
```

Set `-e docker_daemon_allow_restart=true` only on hosts where an automatic
bounce is genuinely safe (json-file Pis with nothing critical running).

Separately: log-driver and log-opts changes only affect newly created
containers regardless of restarts. Existing ones need
`docker compose up -d --force-recreate`.

## Apply

```sh
# Pis (default target)
ansible-playbook \
    playbooks/configure_docker_log_rotation.yml

# One other host, once its host_vars exist
ansible-playbook -e docker_daemon_target=warrig \
    playbooks/configure_docker_log_rotation.yml
```

`docker_daemon_target` defaults to `raspberrypis`. It exists because adding a
group for the Arch docker hosts means editing inventory, which is a separate
change.

### `--limit` cannot substitute for `docker_daemon_target`

This one is worth knowing because it fails silently rather than loudly. The
playbook's `hosts:` is `{{ docker_daemon_target | default('raspberrypis') }}`,
and `--limit` can only narrow a host pattern, never widen it. So:

```sh
# WRONG: intersects warrig with raspberrypis, matches nothing.
# Exits 0 with no recap line for any host. Looks like a pass.
ansible-playbook --limit warrig playbooks/configure_docker_log_rotation.yml

# RIGHT
ansible-playbook -e docker_daemon_target=warrig \
    playbooks/configure_docker_log_rotation.yml
```

The wrong form cost a real verification miss on 2026-08-18: two separate sweeps
"passed" it against warrig and citadel without ever touching either host. An
`rc=0` with no host recap is the tell.

## Upstream

Configures the [Docker Engine](https://github.com/moby/moby) daemon
(Apache-2.0) via `/etc/docker/daemon.json`. On warrig and citadel the
configured driver is the [Grafana Loki Docker driver plugin](https://github.com/grafana/loki)
(AGPL-3.0). This role writes configuration only; it installs neither.
