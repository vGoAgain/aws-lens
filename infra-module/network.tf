# Creating network from https://github.com/vGoAgain/vp-tf-network-module

module "network" {
  source = "git::https://github.com/vGoAgain/vp-tf-network-module.git?ref=v2.0"

  # Required variables
  vpc_cidr = "10.0.0.0/16"
  vpc_name = "vp-tf-network-module"

  subnet_data = {
    "private" = [
      {
        cidr              = "10.0.1.0/24"
        public            = false
        availability_zone = "eu-west-3a"
      },
      {
        cidr              = "10.0.2.0/24"
        public            = false
        availability_zone = "eu-west-3b"
    }],
    "public" = [
      {
        cidr              = "10.0.3.0/24"
        public            = true
        availability_zone = "eu-west-3a"
      },
      {
        cidr              = "10.0.4.0/24"
        public            = true
        availability_zone = "eu-west-3b"
    }]
  }
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "public_subnet_ids" {
  value = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}