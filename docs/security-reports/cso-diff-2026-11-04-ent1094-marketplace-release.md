# /cso --diff — trinity-enterprise#1094 marketplace release scripts

**Date:** 2026-11-04. **Mode:** diff, daily gate (8/10). **Scope:** `packer/digitalocean/scripts/*`, `trinity.pkr.hcl`, README, tests, docs, and the private release skill diff.

## Findings

None at the confidence gate. 0 SUPPORTED.

## Controls challenged

| Control | Evidence |
|---|---|
| Tokens, generated password and JWT stay out of argv | `test_ent1094_marketplace_release.py` asserts on every faked call's argv |
| Credential-in-argv class | `test_292_cmdline_credential_leak.py`: 17 passed |
| TLS verification | No `-k`/`--insecure` in the scripts; `boot-check.sh` ran live against a Let's Encrypt IP certificate |
| Public `/setup` window | Claimed right after backend health; a prior claim or 403 fails the run and destroys the droplet |
| SSH key baked into a snapshot | `90-cleanup.sh` at the pinned commit removes `authorized_keys`; `99-img-check.sh` checks for it and is the last build step |
| Shell injection | Snapshot ID and tag are regex-validated before use; port list comes through a numeric `grep -oE` |
| curl config injection from the sign-in response | Token restricted to JWT characters, with a test |
| Public-repo disclosure | Diff grep: no app ID, team key IDs, IPs, vendor emails or known token prefixes |
| Concealed Unicode | No hits in the diff or the skill file |

## Hypotheses

- `StrictHostKeyChecking=accept-new` trusts the first host key for a fresh droplet. A network attacker at that moment would see a throwaway admin password for a droplet that is destroyed minutes later. Accepted: no durable credential crosses.

## Coverage gaps

- Phases with no diff overlap were skipped: 3 (no dependency changes), 4 (no workflow changes), 6, 7, 8 beyond the Unicode scan, 9–11.
- No image was rebuilt. The diff changes no Dockerfile or compose file.
- `mp-submit.sh` real PATCH and `mp-build.sh` real build were not run; the first real use is the next publish.
- The private skill's rendered commands were run for Step 1 only.
