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

# The build droplet's CPU and RAM do NOT reach the snapshot — a snapshot is a
# disk image, and Trinity never runs during the build (01-provision.sh installs
# and pulls; start.sh runs at first boot on the customer's droplet). The only
# property that propagates is the DISK SIZE, and DigitalOcean does not let it
# shrink: "You can increase the boot disk size when resizing, but you cannot
# decrease it."
#
# So this value decides which plans a customer can deploy the listing to. At
# s-2vcpu-4gb (80 GB disk) every plan with a smaller boot disk is excluded —
# and on DigitalOcean's v5 line disk is decoupled from memory, so that is not
# just the cheap tiers: s5-8vcpu-16gb-30gb is 8 vCPU and 16 GiB RAM on a 30 GiB
# disk, exactly the plan a Trinity operator should pick, and an 80 GB snapshot
# cannot deploy to it. Same shape across the General Purpose and CPU-Optimized
# lines, which are deliberately RAM-rich and disk-poor.
#
# The criterion is therefore the smallest boot disk Trinity's baked images
# actually fit on, with whatever CPU/RAM makes the build fast — NOT the smallest
# plan, and unrelated to the >=8 GB the LISTING recommends for running Trinity.
#
# ponytail: still the 80 GB default, pending the measured size of the baked
# image set (#2281 AC). Drop it to the smallest fitting boot disk once measured.
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

  # Record the snapshot id, then offer it to the Vendor Portal. A `post-processors`
  # (plural) block runs its members as a CHAIN, which is required here: shell-local
  # reads the file manifest writes. Declaring two sibling `post-processor` blocks
  # would run them in parallel and the submit would race the manifest.
  #
  # mp-submit.sh is a no-op unless TRINITY_DO_APP_ID is set, so a plain
  # `packer build` still just builds. See the header of that script.
  post-processors {
    post-processor "manifest" {
      output     = "manifest.json"
      strip_path = true
    }

    post-processor "shell-local" {
      environment_vars = ["TRINITY_IMAGE_TAG=${var.image_tag}"]
      inline           = ["bash scripts/mp-submit.sh"]
    }
  }
}
