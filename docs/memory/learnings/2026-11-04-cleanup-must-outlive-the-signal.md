## 2026-11-04 — pitfall — a cleanup that deletes paid resources must ignore signals and bound its own calls
**Context**: trinity-enterprise#1094. `trap - INT TERM HUP` at the top of a cleanup restored default handling, so one TERM during its diagnostic SSH killed the script before the droplet delete. A hung remote `docker` call stalled the run outside the wait loop's deadline, with the droplet still billing.
**Lesson**: Start the cleanup with `trap '' INT TERM HUP` and give every remote call its own time limit. A deadline on the wait loop covers only that loop.

## 2026-11-04 — pitfall — a closed port can't be proven from a network that answers TCP itself
**Context**: trinity-enterprise#1094. On the operator's network every outbound port connected, including `1.1.1.1:2222`, so a laptop probe called every port open. A packet capture on the droplet showed SYNs in and no SYN-ACKs out.
**Lesson**: Probe from a machine you control next to the target, and require an unused port to read closed. A positive control alone can't catch a vantage that answers everything.

## 2026-11-04 — pitfall — `systemd-run` in cloud-init `bootcmd` wedges boot
**Context**: trinity-enterprise#1094. Stock `ubuntu-24-04-x64` with a `systemd-run --on-active` timer in `bootcmd` never gave SSH, with or without `--no-block`. A control droplet with `runcmd: shutdown -P +45` was up in about a minute. The cause was not established, because no console was available.
**Lesson**: Do not arm a timer from `bootcmd`. Try a change to early boot on a throwaway stock droplet with a control beside it.
