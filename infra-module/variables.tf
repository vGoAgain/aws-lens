variable "prefix" {
  default = "vp"
}

variable "app_name" {
  default = "aws-lens"
}

variable "alb_ingress_rules" {
  type = list(object({
    from_port = number
    to_port   = number
    protocol  = string
    cidr      = string
  }))
  default = [
    { from_port = 443, to_port = 443, protocol = "tcp", cidr = "0.0.0.0/0" },
    { from_port = 80, to_port = 80, protocol = "tcp", cidr = "0.0.0.0/0" }
  ]
}

variable "container_port" {
  default = 5000
  type = number
}

variable "rds_subnet" {
  type = list(map(string))
  default = [{"cidr" = "10.0.5.0/24", "availability_zone" = "eu-west-3a"} , {"cidr" = "10.0.6.0/24", "availability_zone" = "eu-west-3b"}]
  description = "The RDS subnet group"
}