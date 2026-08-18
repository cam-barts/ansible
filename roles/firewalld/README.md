# firewalld

Reconciled against the live rulesets on both hosts on 2026-08-17. This role
was originally authored blind, without sudo to read the real config. It has
since been checked against `sudo firewall-cmd --list-all` on warrig and
citadel, and the model was cut down to match.

The short version: the role is now close to a no-op by design. Everything it
asserts is already true on both hosts, so applying it should report zero
changes. That is the point. It pins the one rule that matters rather than
inventing policy.

`--ask-become-pass` is required, since `ansible.posix.firewalld` always needs
`become` on these password-gated hosts.

## Additive only

Every task uses `state: enabled`. Nothing here ever disables, removes, or
purges a rule, a zone, or a service. It can only open ports that were closed,
and it cannot close anything the live host already has open.

Both hosts also have `dhcpv6-client` enabled in the public zone. This role
does not model it and, being additive only, will not touch it. The role owns
two facts about the zone, not the zone itself.

## Live state, 2026-08-17

Both hosts use the `public` zone as default and active, target `default`, with
no `sources`, no plain `ports`, and no `forward-ports`.

| | warrig | citadel |
|---|---|---|
| interfaces | `eno2 enp3s0 wlan0` | `eno1` |
| services | `dhcpv6-client ssh` | `dhcpv6-client ssh` |
| rich rules | `9100/tcp` from `192.168.1.88` | none |

## The rule model

| rule | port/service | source | hosts | live? |
|------|--------------|--------|-------|-------|
| SSH | `ssh` service, default zone | any | warrig, citadel | already present on both |
| node_exporter scrape | `9100/tcp`, rich rule | citadel (`192.168.1.88`) | warrig only | already present on warrig |
| cadvisor scrape | `8080/tcp`, rich rule | citadel (`192.168.1.88`) | warrig only | added, see below |

SSH is left unrestricted by source, matching the intent of `roles/ufw`'s
rate-limited-but-open SSH rule adapted to firewalld. No `ufw limit` equivalent
is used, since a bare `service: ssh` is the simple option and both hosts are
password-gated for `become`.

The 9100 rule uses `rich_rule` rather than the plain `port` and `source`
params because firewalld's `source` option assigns an entire zone to that
source (a source-restricted zone) rather than scoping one port to one source.
A rich rule is the simple way to say "this port, from this source only." It
works: verified 2026-08-17 that `warrig:9100` answers citadel and refuses
black-pearl.

## cadvisor 8080: only works because warrig uses host networking

A Docker published port is reached through Docker's own iptables chains, which
are consulted before firewalld, so firewalld cannot scope it. Measured on
warrig 2026-08-17 from black-pearl, which no rule permits, while cAdvisor was
still publishing a port:

```
warrig:8080  OPEN     warrig:9100  BLOCKED     warrig:22  OPEN
```

9100 is a host service and firewalld scoped it correctly. 8080 was a container
port and firewalld never saw it, so cAdvisor was answering the whole LAN.

The fix was not a firewall rule, it was moving cAdvisor into the host network
namespace (`cadvisor_network_mode: host` in `host_vars/warrig.yml`). It now
binds 8080 as an ordinary host listener on the INPUT path, which is what puts
it under the rich rule above. See `roles/cadvisor/README.md` for the cutover,
including why the firewall rule has to be applied before the switch.

If warrig's cAdvisor ever returns to a published port, delete this rule rather
than leaving it in place. It would permit nothing and restrict nothing while
reading, in `--list-all`, like policy. That is a bad thing to trust during an
incident.

## One rule that was dropped, and why

### 9100 on citadel: the source never matches

Prometheus runs on citadel as a container at `172.20.0.3` and scrapes
node_exporter, which listens on `*:9100`. That traffic arrives over the docker
bridge with source `172.20.0.3`, not `192.168.1.88`, so a rich rule carrying
citadel's own LAN address would match none of it.

Meanwhile `citadel:9100` is already closed to the LAN. Verified from both
warrig and black-pearl: blocked from each, while `citadel:22` answers both.
The scrape works because of the docker bridge path, not because of any rule
this role would add.

So the rule would have been a no-op that looked like policy. Dropped.

## Usage

```bash
ansible-playbook playbooks/configure_firewalld.yml --ask-become-pass
```

Targets the `firewalld` inventory group (warrig and citadel, in
`inventory/hosts`). Expect zero changed tasks on a reconciled host; a change
means something drifted or a host is new.

Tunables are documented inline in `defaults/main.yml`. Note that ssh is an
unconditional task, separate from the `firewalld_rich_rules` list.

## Upstream

Configures [firewalld](https://firewalld.org/) (GPL-2.0-or-later) through the
`ansible.posix.firewalld` module from the
[ansible.posix](https://github.com/ansible-collections/ansible.posix)
collection (GPL-3.0). This role only manages rules; it does not install or
modify firewalld itself.
