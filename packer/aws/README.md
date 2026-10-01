# Trinity — AWS Marketplace AMI and CloudFormation template

Packer build for the Trinity AWS Marketplace listing (#3004, epic #2332), plus the
CloudFormation template behind the README's Launch Stack button.

## What this produces

A public x86_64 AMI in **us-east-1**: Ubuntu 24.04 (Canonical, owner
`099720109477`), IMDSv2 required, ENA, an unencrypted 25 GB gp3 root volume, and
a pinned Trinity release with every image already pulled. An instance launched
from it serves Trinity over browser-trusted HTTPS on its public IP a few minutes
after launch, with no user data.

| Stage | What happens |
|---|---|
| Build | `packer/digitalocean/scripts/01-provision.sh` with `TRINITY_CLOUD=aws`: Trinity checkout at `/opt/trinity`, `start.sh --provision --cloud aws --machine-only` (Docker, pinned Caddy, ufw, the firewall unit), all images pulled at the pinned tag, `aws` recorded in `/etc/trinity/cloud` |
| Cleanup | `scripts/90-aws-cleanup.sh`, the last provisioner: removes keys, history, host keys, logs and cloud-init state, puts back the first-boot hook `cloud-init clean` deletes, and fails the build unless every check passes |
| First boot | `firstboot.sh` (shared with DigitalOcean) reads `aws`, exports `ADMIN_PASSWORD_SOURCE=instance-id`, then `start.sh --provision --cloud aws --site-only --provenance aws-marketplace --hosted --unattended`: public IP from IMDSv2, the instance-ID claim file, `.env`, the Caddyfile, a certificate for the IP, the refresh timer, and the install |
| Every 5 min | `trinity-ip-refresh.timer`: when the public IP changed (stop/start, a late Elastic IP), rewrites the Caddyfile, the certificate and `FRONTEND_URL` (only while it still points at the old IP) and recreates the backend from the images it already has; otherwise silent |

The files tree and the build script are the DigitalOcean bundle's, referenced
from `../digitalocean/`, not copied: a second copy is how the port list drifted
before (#2380). Only the builder, the cleanup and the submit script are AWS-only.

## Prerequisites

- The release's images on GHCR, publicly pullable.
- `packer` ≥ 1.9 and AWS credentials for the publishing account (environment or
  profile) with EC2 image-building permissions in us-east-1.
- The account's EC2 **Block public access for AMIs** setting off, so `ami_groups =
  ["all"]` can make the AMI public. Newer accounts have it on by default, and the
  build then fails at the share step after the whole build. Check and turn it off
  (us-east-1):

  ```bash
  aws ec2 get-image-block-public-access-state --region us-east-1
  aws ec2 disable-image-block-public-access --region us-east-1
  ```

  To build a private AMI for testing instead, remove the `ami_groups` line. The
  Marketplace lane does not need a public AMI; the Launch Stack lane does.

## Build

```bash
packer init trinity.pkr.hcl
packer build -var "image_tag=<release tag>" trinity.pkr.hcl
```

`image_tag` is required, may not be `latest`, and must be the **first release
containing AWS support** (#3004) or later — an older checkout's `start.sh`
rejects `--cloud aws`, and the build stops right after the clone if it finds no
AWS support. The build instance's temporary
security group admits SSH from this machine's address only.

## Submitting a version

`scripts/mp-submit.sh` runs after every build and does nothing unless
`TRINITY_AWS_PRODUCT_ID` is set. With it set it sends an `AddDeliveryOptions`
change set for `AmiProduct@1.0` through the Catalog API as a **dry run**
(`Intent=VALIDATE`); add `TRINITY_AWS_SUBMIT=apply` to submit.

```bash
TRINITY_AWS_PRODUCT_ID=<product id> \
TRINITY_AWS_INGESTION_ROLE_ARN=<ingestion role ARN> \
  packer build -var "image_tag=<release tag>" trinity.pkr.hcl
```

The ingestion role trusts AWS Marketplace ("Marketplace – AMI Assets
Ingestion") with the `AWSMarketplaceAmiIngestion` policy. Run "Test Add Version"
(the self-service scan) on a release-candidate AMI before the first submission.

## CloudFormation

`trinity.cfn.yaml` creates the instance (default `t3a.large`), a security group
open on 80 and 443, an Elastic IP, and optionally SSH from one address range
(`SshCidr`) with a key pair (`KeyName`). No IAM role. The instance goes into a
default subnet of the default VPC, or into `SubnetId` (with its `VpcId`), and
always gets a public IPv4 address. Outputs: `TrinityUrl` and
`InstanceId` — the value `/setup` asks for.

Host the template in a public S3 bucket, then build the quick-create link:

```
https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?templateURL=<TEMPLATE_URL>&stackName=trinity&param_ImageId=<AMI_ID>
```

The Launch Stack button in the top-level README is commented out until both
values exist. The AMI is in us-east-1 only, so the link is for that region.

| Release | AMI (us-east-1) |
|---|---|
| — | not yet published |

## Security notes

- Containers cannot reach the metadata service: `scripts/deploy/docker-firewall.sh`
  drops `169.254.0.0/16` from containers ahead of its bridge RETURNs.
- The admin is created only by someone who can read the instance ID from the EC2
  console (the setup-claim file); nothing is preset and no user data is read.
- ufw allows 22 on the host; the security group is what keeps it closed.
- On a script install, `trinity-ip-refresh.service` runs the checkout's
  `scripts/deploy/start.sh` as root every five minutes, so the checkout must be
  owned by root and writable by nobody else. The AMI's `/opt/trinity` is.

## Not verified without AWS

The template passes `packer validate` and the scripts pass `bash -n`, but none of
this has run on EC2 yet: the build, the Marketplace scan, first boot, the refresh
after a stop/start, the Elastic IP timing, and the container block on the
metadata service. Check each on a release-candidate image before submitting.
