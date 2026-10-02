# AWS Marketplace AMI for Trinity (#3004).
#
# Build (AWS credentials from the usual environment / profile):
#   packer init trinity.pkr.hcl
#   packer build -var "image_tag=<release tag>" trinity.pkr.hcl
#
# The tag must be the first release containing AWS support (#3004) or later:
# older checkouts' start.sh rejects `--cloud aws`, and 01-provision.sh stops the
# build right after the clone if it finds no AWS support.
#
# The same bundle as the DigitalOcean 1-Click (packer/digitalocean/): the files
# tree and 01-provision.sh are referenced from there, not copied, and run with
# TRINITY_CLOUD=aws. 01-provision.sh bakes that value into /etc/trinity/cloud,
# and first boot reads it back to take the AWS admin path (the instance-ID
# claim) and the `aws-marketplace` provenance. What is AWS-specific lives here:
# the builder, and the cleanup that replaces DigitalOcean's 90-cleanup /
# 99-img-check.
#
# What happens at BUILD time (baked into the AMI):
#   - Docker + Caddy + ufw + the container firewall unit (start.sh --machine-only)
#   - the Trinity checkout at /opt/trinity, all images pulled at a PINNED tag
#
# What happens at FIRST BOOT (per instance):
#   - ADMIN_PASSWORD_SOURCE=instance-id; the instance ID (IMDSv2) is written to
#     the setup-claim file and /setup asks for it before creating the admin
#   - .env written, including TRINITY_INSTALL_SOURCE=aws-marketplace
#   - Caddy issued a Let's Encrypt certificate for the instance's public IP
#   - trinity-ip-refresh.timer follows a changed IP after a stop/start
#
# image_tag is REQUIRED and pinned, never `latest`, for the reason the DO
# template gives: Marketplace review approves one artifact.

packer {
  required_plugins {
    amazon = {
      version = "~> 1.3"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "image_tag" {
  type        = string
  description = "Trinity release tag to bake, containing AWS support (#3004). Never 'latest'."
  validation {
    condition     = var.image_tag != "latest" && length(var.image_tag) > 0
    error_message = "The image_tag variable must be a pinned release tag, not 'latest'."
  }
}

# The build instance's size does not reach the AMI; only the root volume does.
variable "build_instance_type" {
  type    = string
  default = "t3a.medium"
}

locals {
  # Minute resolution, so two builds of one tag on one day stay distinguishable
  # in the Marketplace "Add version" picker.
  ami_name = "trinity-${replace(var.image_tag, ".", "-")}-${formatdate("YYYYMMDD-hhmm", timestamp())}"
}

source "amazon-ebs" "trinity" {
  # A Marketplace AMI must be submitted from us-east-1; Marketplace copies it to
  # the other regions. The CloudFormation lane uses the AMI in this region only.
  region        = "us-east-1"
  instance_type = var.build_instance_type
  ssh_username  = "ubuntu"

  source_ami_filter {
    filters = {
      name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
      architecture        = "x86_64"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["099720109477"] # Canonical
    most_recent = true
  }

  ami_name        = local.ami_name
  ami_description = "Trinity ${var.image_tag} - sovereign AI agent platform on Ubuntu 24.04"
  # Public, so the CloudFormation Launch Stack works outside the build account.
  # The account's "block public access for AMIs" setting must be off for this
  # to take effect.
  ami_groups = ["all"]

  # Instances launched from the AMI require IMDSv2 (hop limit 2).
  imds_support = "v2.0"
  ena_support  = true

  # The build instance itself: IMDSv2 only, and SSH open to this machine's own
  # address rather than the whole internet for the length of the build.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
  }
  temporary_security_group_source_public_ip = true

  # Marketplace rejects encrypted snapshots. 25 GB: a DigitalOcean droplet from
  # the same bundle measures 8.8 GB used with Trinity running.
  encrypt_boot = false
  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 25
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = false
  }

  tags = {
    Name              = local.ami_name
    trinity_image_tag = var.image_tag
  }
}

build {
  sources = ["source.amazon-ebs.trinity"]

  # FIRST: wait for cloud-init, which holds the dpkg lock on a fresh Ubuntu
  # image (see packer/digitalocean/trinity.pkr.hcl for the failure this
  # prevents), and create the file provisioner's destination so the upload
  # keeps its opt/ etc/ var/ layout.
  provisioner "shell" {
    inline = [
      "cloud-init status --wait",
      "mkdir -p /tmp/trinity-files",
    ]
  }

  provisioner "file" {
    source      = "${path.root}/../digitalocean/files/"
    destination = "/tmp/trinity-files"
  }

  # The Ubuntu AMI logs in as `ubuntu`, not root: every script runs under sudo.
  # A custom execute_command replaces Packer's default, which is where
  # environment_vars are injected, so `{{ .Vars }}` must be passed explicitly or
  # TRINITY_CLOUD / TRINITY_IMAGE_TAG never reach the script.
  provisioner "shell" {
    environment_vars = [
      "TRINITY_IMAGE_TAG=${var.image_tag}",
      "TRINITY_CLOUD=aws",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    execute_command = "chmod +x {{ .Path }}; sudo -E env {{ .Vars }} bash '{{ .Path }}'"
    scripts         = ["${path.root}/../digitalocean/scripts/01-provision.sh"]
  }

  # LAST provisioner. It removes what must not be shared by every instance and
  # then fails the build if any of it is still there.
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; sudo -E env {{ .Vars }} bash '{{ .Path }}'"
    scripts         = ["${path.root}/scripts/90-aws-cleanup.sh"]
  }

  # Record the AMI id; mp-submit.sh is a no-op unless TRINITY_AWS_PRODUCT_ID is
  # set. `post-processors` (plural) runs them as a chain, so the submit reads
  # the manifest the first one wrote.
  post-processors {
    post-processor "manifest" {
      output     = "manifest.json"
      strip_path = true
    }

    post-processor "shell-local" {
      environment_vars = ["TRINITY_IMAGE_TAG=${var.image_tag}"]
      inline           = ["bash ${path.root}/scripts/mp-submit.sh"]
    }
  }
}
