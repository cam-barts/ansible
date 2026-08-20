# compose_stack

Byte-faithful tracker and reconciler for one running docker compose stack.
Templates or copies the stack's compose file and env files to their live
paths, then reconciles the containers to them: a wiped file comes back, a
down stack comes up, and a converged stack reports zero changes.

## What the role does

- Renders every file listed in the host's
  `compose_stacks[compose_stack_name].files` to the stack dir, with
  `backup: true`. Sources ending `.j2` go through `template` (`diff: false`,
  so `--diff` can never print a secret); everything else goes through
  `copy`, which does no Jinja pass — `${VAR}` interpolation and literal
  `{{ }}` in upstream files survive by construction. Only secret-bearing
  files are templates.
- Runs `docker_compose_v2` with `state: {{ compose_stack_state }}`
  (default `present`, i.e. `up -d`). Override per run to change container
  state deliberately:

      ansible-playbook playbooks/configure_compose_immich.yml                                  # files + up
      ansible-playbook playbooks/configure_compose_immich.yml -e compose_stack_state=absent    # down (volumes kept)
      ansible-playbook playbooks/configure_compose_immich.yml -e compose_stack_state=stopped   # stop
      ansible-playbook playbooks/configure_compose_immich.yml -e compose_stack_state=restarted # bounce

- `pull: missing`, deliberately: windmill and surrealdb set
  `pull_policy: always` in their compose files, which would otherwise turn
  every play run into an image upgrade. Watchtower owns image upgrades in
  this lab; this role owns state. An image Watchtower already pulled still
  reconciles here (the running container no longer matches it).

Scope is the compose file, the env files compose reads, and every
SINGLE-FILE bind mount the stack's containers carry (prometheus.yml,
loki-config.yaml, Caddyfile, garage.toml, clickhouse XMLs, dashy's
conf.yml, wishthis's config.php, …) — the files a stack needs present to
start. Directory bind mounts (searxng's ./searxng/, grafana provisioning,
data dirs) are deliberately NOT tracked; the role recreates missing
subdirectories for its tracked files but never reconstitutes whole config
trees. One deliberate skip: ansible-semaphore's semaphore-config/gitconfig
is owned by uid 1001/root and not writable by nux.

## The loki teardown hazard

citadel's and warrig's dockerd default to the `loki` log driver, where
removing a RUNNING container can deadlock the daemon
(roles/cadvisor/README.md). Compose's recreate and `down` paths stop
containers before removing them — the safe side of that hazard, and the
same path Watchtower exercises daily. It is still a recreate: expect a
brief restart when a stack has genuinely drifted.

A per-stack `reconcile: false` in the stack entry skips the container task
entirely (files-only). Reserved for stacks where reconciling is impossible;
currently only wellerman's apprise-api (compose-v1 relic whose image no
longer builds). Every use must carry a justifying comment in host_vars.

## Requirements

The connecting user owns the stack files and is in the docker group; nothing
here needs root (playbooks set `ansible_become: false` as a play VAR — the
keyword loses to inventory, see the repo README precedence trap).

## Variables

See `defaults/main.yml`. The per-host stack catalog lives in
`inventory/host_vars/<host>/vars.yml` as `compose_stacks`, a dict keyed by
stack name, plus a single `compose_stack_root`. An entry only carries what
deviates from convention: `project` defaults to the dict key (never the
directory basename — webhooksite's dir is `webhook/webhook.site`), `dir`
defaults to `<compose_stack_root>/<name>`, `owner`/`group` default to the
connecting user. The `files` list of `{src, dest, mode}` is always explicit.
Secrets render from passthrough vars
(`<stack>_<key>: "{{ vault_<stack>_<key> }}"`) backed by the host's encrypted
`vault.yml`.

## Byte-fidelity

Every source file is a byte-identical capture of the live file (verified with
`cmp` at authoring time), except that files carrying a secret are tightened to
mode 0640 — file mode is not part of compose's config hash, so that recreates
nothing (measured on bunkerweb).

## Usage

One playbook per service: `playbooks/configure_compose_<stack>.yml` sets
`compose_stack_name` and targets the host(s) that run it.

    ansible-playbook --check --diff playbooks/configure_compose_immich.yml
    ansible-playbook playbooks/configure_compose_immich.yml
