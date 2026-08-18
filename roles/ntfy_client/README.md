# ntfy_client

System-wide `ntfy` CLI config pointing at Cam's self-hosted ntfy server.

## What it does

Renders `/etc/ntfy/client.yml` (root-owned, mode 0644) with a `default-host`
pointing at the self-hosted server. Without this the `ntfy` CLI silently
defaults to the public `https://ntfy.sh`, so any host missing the config leaks
notifications onto a public topic.

## What it does NOT do

- Install the `ntfy` CLI itself, which is platform-specific. On Arch:
  `pacman -S ntfysh-bin`. On Synology: manual binary from
  https://github.com/binwiederhier/ntfy/releases. On Debian/Hypriot Pis:
  `apt install ntfy` (if packaged) or manual binary.

## Inputs

| Variable | Default | Notes |
|---|---|---|
| `ntfy_client_default_host` | `https://ntfy.coder.cam` | Self-hosted server URL |
| `ntfy_client_default_user` | undefined | Optional auth username |
| `ntfy_client_default_password` | undefined | Optional auth password |
| `ntfy_client_default_token` | undefined | Optional auth token |

## Used by

- `borgmatic_citadel`, which declares the dependency in `meta/main.yml`
- `rsnapshot_gastown`, which should declare it once gastown is converted to
  the system-wide config instead of the wrapper-embedded path
- Any future role or host that calls `ntfy` from a script or systemd unit

## Upstream

Configures the CLI shipped with [ntfy](https://github.com/binwiederhier/ntfy),
dual-licensed Apache-2.0 or GPL-2.0. This role writes `/etc/ntfy/client.yml`
only; installing the CLI is out of scope, as noted above.
