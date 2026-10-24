# One-click images of Valeria One for cloud marketplaces (DigitalOcean, Vultr).
#
#   cd deploy/marketplace/packer
#   packer init .
#   DIGITALOCEAN_TOKEN=… packer build -only=digitalocean.valeria-one .
#   VULTR_API_KEY=…      packer build -only=vultr.valeria-one .
#
# The image has Docker, the code and the built app, and NO secrets: each new server makes its own keys and
# database password on first boot (valeria-one-firstboot.service), then prints its setup link at login.

packer {
  required_plugins {
    digitalocean = {
      version = ">= 1.4.0"
      source  = "github.com/digitalocean/digitalocean"
    }
    vultr = {
      version = ">= 2.5.0"
      source  = "github.com/vultr/vultr"
    }
  }
}

variable "do_token" {
  type      = string
  default   = env("DIGITALOCEAN_TOKEN")
  sensitive = true
}

variable "vultr_api_key" {
  type      = string
  default   = env("VULTR_API_KEY")
  sensitive = true
}

variable "ref" {
  type        = string
  default     = ""
  description = "Release tag to bake in (empty: the newest release)."
}

variable "vultr_os_id" {
  type        = number
  default     = 2284
  description = "Ubuntu 24.04 LTS x64 (GET https://api.vultr.com/v2/os)."
}

locals {
  stamp = formatdate("YYYYMMDD-hhmm", timestamp())
  ref   = var.ref == "" ? "" : "--ref ${var.ref}"
}

source "digitalocean" "valeria-one" {
  api_token     = var.do_token
  image         = "ubuntu-24-04-x64"
  region        = "nyc3"
  size          = "s-1vcpu-1gb" # build on the smallest Droplet so every size can use the image (DigitalOcean rule)
  ssh_username  = "root"
  snapshot_name = "valeria-one-${local.stamp}"
}

source "vultr" "valeria-one" {
  api_key              = var.vultr_api_key
  os_id                = var.vultr_os_id
  plan_id              = "vc2-1c-1gb" # smallest plan, so every size can use the snapshot
  region_id            = "ewr"
  snapshot_description = "valeria-one-${local.stamp}"
  ssh_username         = "root"
  state_timeout        = "30m"
}

build {
  sources = ["source.digitalocean.valeria-one", "source.vultr.valeria-one"]

  provisioner "shell" {
    inline = ["cloud-init status --wait || true"]
  }

  provisioner "file" {
    source      = "../../install.sh"
    destination = "/root/valeria-one-install.sh"
  }

  provisioner "shell" {
    environment_vars = ["DEBIAN_FRONTEND=noninteractive"]
    inline = [
      "apt-get update -qq && apt-get -y -qq -o Dpkg::Options::=--force-confold upgrade",
      "bash /root/valeria-one-install.sh --image ${local.ref}",
      "rm -f /root/valeria-one-install.sh",
    ]
  }

  provisioner "shell" {
    scripts = ["scripts/90-cleanup.sh"]
  }

  # DigitalOcean's own checks for marketplace images
  provisioner "shell" {
    only   = ["digitalocean.valeria-one"]
    inline = ["curl -fsSL https://raw.githubusercontent.com/digitalocean/marketplace-partners/master/scripts/99-img-check.sh | bash"]
  }
}
