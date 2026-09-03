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
}

build {
  sources = ["source.digitalocean.trinity"]

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
