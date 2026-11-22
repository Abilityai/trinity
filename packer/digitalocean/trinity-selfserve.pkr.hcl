# Self-serve snapshot for Trinity on DigitalOcean (trinity-enterprise#1347).
#
# Build:  bash scripts/selfserve-build.sh <commit>     (see README.md → Self-serve snapshot)
#
# A sibling of trinity.pkr.hcl, the Marketplace 1-Click. That template, its
# first boot and its output are unchanged. This one differs in three ways:
#
#   - it bakes ONE commit, not a release tag: the checkout is that commit and
#     the images are the `sha-<short7>` tags publish-images.yml pushes for it;
#   - the enterprise module is checked out at that commit's pin, fetched with a
#     build-time token that never reaches the disk;
#   - first boot (selfserve/firstboot.sh) takes the instance's public name from
#     user-data, sets up claim-based setup with no claim yet, records the
#     install source as `script` and starts. The claim arrives later over SSH.
#
# The snapshot holds the private module. Keep it private to the account that
# builds it: never share it or submit it to a listing.

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

variable "commit" {
  type        = string
  description = "Full 40-character SHA of the platform commit to bake."
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.commit))
    error_message = "The commit variable must be a full 40-character lowercase commit SHA."
  }
}

variable "enterprise_git_token" {
  type        = string
  sensitive   = true
  description = "Read-only token for the enterprise module's repository. Used during the build only."
  validation {
    condition     = length(var.enterprise_git_token) > 0
    error_message = "The enterprise_git_token variable is required: the snapshot carries the enterprise module."
  }
}

# See trinity.pkr.hcl: a pre-made key avoids DigitalOcean's key-id race.
variable "ssh_key_id" {
  type    = number
  default = 0
}

variable "ssh_private_key_file" {
  type    = string
  default = ""
}

variable "run_tag" {
  type    = string
  default = ""
}

# The snapshot is created in this region. Build where instances are created, or
# transfer it afterwards.
variable "region" {
  type    = string
  default = "sfo2"
}

# Only the disk size reaches the snapshot (see trinity.pkr.hcl).
variable "build_size" {
  type    = string
  default = "s-1vcpu-1gb"
}

locals {
  image_tag     = "sha-${substr(var.commit, 0, 7)}"
  snapshot_name = "trinity-selfserve-${substr(var.commit, 0, 7)}-${formatdate("YYYYMMDD-hhmm", timestamp())}"
}

source "digitalocean" "trinity_selfserve" {
  api_token     = var.do_token
  image         = "ubuntu-24-04-x64"
  region        = var.region
  size          = var.build_size
  ssh_username  = "root"
  snapshot_name = local.snapshot_name

  ssh_key_id           = var.ssh_key_id
  ssh_private_key_file = var.ssh_private_key_file

  tags = var.run_tag == "" ? [] : [var.run_tag]
}

build {
  sources = ["source.digitalocean.trinity_selfserve"]

  # Same opening as trinity.pkr.hcl: wait for cloud-init's apt runs, and create
  # the upload destinations first so the `file` provisioners keep the layout.
  provisioner "shell" {
    inline = [
      "cloud-init status --wait",
      "mkdir -p /tmp/trinity-files /tmp/trinity-selfserve",
    ]
  }

  provisioner "file" {
    source      = "files/"
    destination = "/tmp/trinity-files"
  }

  provisioner "file" {
    source      = "selfserve/firstboot.sh"
    destination = "/tmp/trinity-selfserve/firstboot.sh"
  }

  # The commit, with the enterprise module at its pin. The token is passed in
  # this step's environment only.
  provisioner "shell" {
    environment_vars = [
      "TRINITY_COMMIT=${var.commit}",
      "TRINITY_ENTERPRISE_TOKEN=${var.enterprise_git_token}",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    scripts = ["selfserve/10-checkout.sh"]
  }

  # The Marketplace's own bakery: machine phase, image pulls, cloud-init hook,
  # MOTD. With TRINITY_CHECKOUT_READY=1 it uses the checkout above instead of
  # cloning a release tag.
  provisioner "shell" {
    environment_vars = [
      "TRINITY_IMAGE_TAG=${local.image_tag}",
      "TRINITY_CHECKOUT_READY=1",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    scripts = ["scripts/01-provision.sh"]
  }

  provisioner "shell" {
    environment_vars = ["TRINITY_COMMIT=${var.commit}"]
    scripts          = ["selfserve/80-finish.sh"]
  }

  # DigitalOcean's cleanup (shell history, logs, host keys, authorized_keys,
  # cloud-init state) and img_check.
  provisioner "shell" {
    scripts = ["scripts/90-cleanup-and-check.sh"]
  }

  # Read-only, so it cannot undo what cleanup removed. Fails the build unless
  # the snapshot is clean and is the commit it claims to be.
  provisioner "shell" {
    scripts = ["selfserve/95-assert-clean.sh"]
  }

  post-processor "manifest" {
    output     = "manifest-selfserve.json"
    strip_path = true
  }
}
