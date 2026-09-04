# DigitalOcean Marketplace 1-Click snapshot for Trinity (#2281).
#
# Build:  packer build -var "do_token=$DIGITALOCEAN_TOKEN" -var "image_tag=v0.9.1" trinity.pkr.hcl
#
# The snapshot is BUILT from prebuilt GHCR images (#2280) rather than from
# source. A 1-Click that spends 5-10 minutes compiling the agent base image on
# first boot fails the one-click bar, which is the whole reason #2280 gates this
# issue.
#
# What happens at BUILD time (baked into the snapshot):
#   - Docker + Caddy + ufw installed
#   - the Trinity checkout placed at /opt/trinity
#   - all five images pulled at a PINNED tag, agent base retagged locally
#
# What happens at FIRST BOOT (per droplet, see files/.../per-instance):
#   - admin password resolved (user-data-supplied, else generated)
#   - .env written, including TRINITY_INSTALL_SOURCE=do-marketplace
#   - Caddy issued a Let's Encrypt certificate for the droplet's own IP
#   - `start.sh --hosted --unattended` brings the stack up
#
# image_tag is deliberately REQUIRED and pinned, never `latest`: a snapshot that
# resolves `latest` at first boot would serve a different Trinity on every
# droplet created from one reviewed image, and Marketplace review approves a
# specific artifact.

packer {
  required_plugins {
    digitalocean = {
      version = ">= 1.4.0"
      source  = "github.com/digitalocean/digitalocean"
    }
  }
}

variable "do_token" {
  type      = string
  sensitive = true
}

variable "image_tag" {
  type        = string
  description = "Trinity release tag to bake, e.g. v0.9.1. Never 'latest'."
  validation {
    condition     = var.image_tag != "latest" && length(var.image_tag) > 0
    error_message = "The image_tag variable must be a pinned release tag, not 'latest'."
  }
}

# Optional: build with an SSH key that ALREADY exists on the account, instead of
# letting the builder import a temporary one per run.
#
# Left unset (the default) the builder imports a key and creates the droplet in
# the next API call — and DigitalOcean does not reliably resolve the new key id
# that fast. The first real build of this bundle lost that race on 4 of 5
# creates, each failing in ~7 seconds with
# "422 ... <id> are invalid key identifiers for Droplet creation", and each
# leaking the temporary key (Packer's own cleanup then 404s on it).
#
# Supplying a pre-made key removes the window entirely, because nothing is
# created during the build. Both must be given together; `ssh_key_id = 0` and an
# empty path mean "unset", which is exactly the old behaviour.
#
#   doctl compute ssh-key import trinity-packer-build --public-key-file ~/.ssh/id_ed25519.pub
variable "ssh_key_id" {
  type        = number
  default     = 0
  description = "ID of an existing DigitalOcean SSH key. 0 = let Packer create a temporary one."
}

variable "ssh_private_key_file" {
  type        = string
  default     = ""
  description = "Private key matching ssh_key_id. Required when ssh_key_id is set."
}

variable "region" {
  type    = string
  default = "nyc3"
}

# DO's own guidance builds on the smallest size; the snapshot is size-agnostic.
# The LISTING recommends 8 GB — that is a droplet-plan recommendation in the
# listing copy, unrelated to what the image is built on.
variable "build_size" {
  type    = string
  default = "s-2vcpu-4gb"
}

locals {
  snapshot_name = "trinity-${replace(var.image_tag, ".", "-")}-${formatdate("YYYYMMDD", timestamp())}"
}

source "digitalocean" "trinity" {
  api_token     = var.do_token
  image         = "ubuntu-24-04-x64"
  region        = var.region
  size          = var.build_size
  ssh_username  = "root"
  snapshot_name = local.snapshot_name

  # Both zero-valued unless the operator supplied them; the builder then falls
  # back to importing a temporary key, which is the pre-existing behaviour.
  ssh_key_id           = var.ssh_key_id
  ssh_private_key_file = var.ssh_private_key_file
}

build {
  sources = ["source.digitalocean.trinity"]

  # FIRST, before anything touches apt. Ubuntu's cloud images run apt-daily and
  # unattended-upgrades on boot, and they hold /var/lib/dpkg/lock-frontend while
  # Packer's SSH session is already open — so 01-provision.sh's opening
  # `apt-get update` races them and the build dies with
  # "E: Could not get lock /var/lib/dpkg/lock-frontend" (apt exit 100).
  # Intermittent, so it reads as a flake; it is not one, and a build that only
  # succeeds sometimes is not something to hand a Marketplace reviewer.
  #
  # This is DigitalOcean's own prescribed remedy, not an invention: their
  # reference template (marketplace-partners/marketplace-image.json, the same
  # repo 90-cleanup-and-check.sh already pins) opens with exactly this
  # provisioner. Ours had no wait of any kind.
  #
  # It belongs in the template rather than at the top of 01-provision.sh because
  # the `file` provisioner below also runs before that script.
  provisioner "shell" {
    inline = [
      "cloud-init status --wait",
      # Create the file provisioner's destination BEFORE it runs. This is not
      # tidiness — with the destination absent, Packer flattens the upload and
      # strips the top-level directory names, so `files/opt/trinity-firstboot/
      # firstboot.sh` lands at `/tmp/trinity-files/trinity-firstboot/
      # firstboot.sh` and every `install /tmp/trinity-files/opt/...` below fails
      # with "cannot stat". Verified both ways against a live droplet: absent,
      # the tree comes up as trinity-firstboot/ update-motd.d/ systemd/ lib/;
      # present, it comes up as opt/ etc/ var/ exactly as the bundle is laid out.
      "mkdir -p /tmp/trinity-files",
    ]
  }

  # Files first: the per-instance script and MOTD must exist before cleanup runs.
  provisioner "file" {
    source      = "files/"
    destination = "/tmp/trinity-files"
  }

  provisioner "shell" {
    environment_vars = ["TRINITY_IMAGE_TAG=${var.image_tag}", "DEBIAN_FRONTEND=noninteractive"]
    scripts          = ["scripts/01-provision.sh"]
  }

  # DO's own validation and cleanup, fetched from digitalocean/marketplace-partners.
  # cleanup MUST run before img_check, and img_check MUST be the last thing that
  # touches the droplet — anything after it can reintroduce exactly what it
  # verified was gone (shell history, logs, host keys).
  provisioner "shell" {
    scripts = ["scripts/90-cleanup-and-check.sh"]
  }
}
