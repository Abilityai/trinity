# Trinity on the Vultr Marketplace (imageless)

The listing is **imageless**: Vultr runs [`vultr-vendor-data.sh`](vultr-vendor-data.sh)
once, as root, on a clean Ubuntu 24.04 at first boot. There is no snapshot to build,
review or resubmit — which is why this lane costs one script instead of a Packer
pipeline, and why first boot takes about 4 minutes (apt + a ~1.6 GB image pull) rather
than the DigitalOcean image's ~2 — measured end to end on a `vc2-4c-8gb` in Frankfurt.

The script resolves the **latest stable release** at boot (`releases/latest`, which
excludes release candidates). Nothing here carries a version, so no release requires
re-pasting anything into the portal.

Ubuntu is not incidental: `start.sh --provision` installs Docker from Docker's
Ubuntu apt repository.

---

## App Instructions (paste into the Vendor Portal)

> ## Trinity is starting
>
> Your server is running, but Trinity is **still installing** — it downloads and
> starts about 1.6 GB of images on first boot. Give it **about 5 minutes**.
>
> ## Open it
>
> https://{{ip}}
>
> The **first person to open that address creates the admin account** — email and
> password, in the browser. Until someone does, anyone who finds the address can.
> So open it now.
>
> ## Before you deploy
>
> - **8 GB RAM minimum.** Below that, agents and the platform contend for memory and
>   turns start failing under load. Vultr cannot enforce a plan size, so please check.
> - **IPv4 is required.** Trinity's certificate is issued for the server's own IPv4
>   address.
>
> ## If it does not come up
>
> SSH in as root and read the log:
>
>     tail -100 /var/log/trinity-install.log
>
> The login banner also reports what happened, and prints the retry command.
>
> ## Next steps
>
> Point a domain at this server and save it under Settings → General, then put a
> Cloudflare Tunnel in front of it. Trinity walks you through both on first run.
>
> Docs: https://docs.ability.ai · Issues: https://github.com/abilityai/trinity/issues
> Vultr does not build or support Trinity.

---

## Runbook

### Creating or updating the build

1. App → **Builds** → **Build from Vendor Data**, base OS **Ubuntu 24.04**.
2. Paste `scripts/deploy/vultr-vendor-data.sh` **from a release tag**, not from `dev`.
3. Deploy the private build (**Deploy Image**) on a plan with at least 8 GB and check:
   - `/var/log/trinity-install.log` ends with `=== Trinity is ready at https://<ip> ===`
   - `https://<ip>` serves with a valid certificate
   - `/setup` creates the admin, and an agent can be created
   - Settings → About reports install source **Vultr Marketplace**
4. Publish the build. Only one build can be Live, so unpublish the current one first.

Only step 2's *script text* is versioned here. Because the script resolves the release
itself, a Trinity release needs **no** portal change — customers get the new release on
their next deploy. Re-paste only when this script changes.

### Going public

Vultr requires a published build, both icons, a description, a README, instructions and
a support email or URL. **Make app public** starts their review; approval arrives by
email.

### Retry on a failed boot

```bash
cd /opt/trinity && sudo ./scripts/deploy/start.sh \
    --provision --cloud vultr --provenance vultr-marketplace --hosted --unattended
```

The clone is guarded, so this resumes rather than starting over. Do **not** run
`cloud-init clean` — it also re-runs Vultr's own instance configuration.

`/var/log/trinity-install.log` is appended across attempts, so an earlier
`FAILED` line sits above a later success. Read from the last
`=== Trinity first boot: <timestamp> ===` header down, or just check the
markers, which the script clears on every entry: `/etc/trinity/ready` versus
`/etc/trinity/firstboot-failed`.

### Testing without a vendor account

The same file works as ordinary **User Data** on any Vultr instance, which is how the
install path is exercised before the listing exists. Provenance still records
`vultr-marketplace`, so use a throwaway instance rather than a real deployment.
