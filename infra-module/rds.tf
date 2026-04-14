resource "aws_subnet" "tf-rds-subnets" {
  for_each          = var.rds_subnet_list
  vpc_id            = module.network.vpc_id
  cidr_block        = each.value[0].cidr
  availability_zone = each.value[0].availability_zone
  tags = {
    "Name" = "${var.prefix}-rds-subnet-${each.key}"
  }

}

# subnet group
resource "aws_db_subnet_group" "tf-rds-subnet-group" {
  name       = "${var.prefix}-${var.app_name}-db-subnet-group"
  subnet_ids = values(aws_subnet.tf-rds-subnets)[*].id
}

# password -> aws secrets manager
resource "random_password" "rds-db-password" {
  length           = 12
  special          = false
  override_special = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
}

#secret manager create 
resource "aws_secretsmanager_secret" "db-password" {
  name = "creds4/awslens/db"
}

# secret manager version
resource "aws_secretsmanager_secret_version" "rds-db-secret-version" {
  secret_id = aws_secretsmanager_secret.db-password.id
  secret_string = jsonencode({
    username = aws_db_instance.postgres.username
    password = random_password.rds-db-password.result
    host     = aws_db_instance.postgres.address
    port     = aws_db_instance.postgres.port
    dbname   = aws_db_instance.postgres.db_name
  })

  depends_on = [aws_db_instance.postgres]
}

# rds instance
resource "aws_db_instance" "postgres" {
  identifier = "${var.prefix}-${var.app_name}-postgres-db"

  engine         = "postgres"
  engine_version = "16.13"
  instance_class = "db.t3.micro"

  allocated_storage = 20
  storage_type      = "gp2"

  db_name  = "awslensdb"
  username = "vaishakhprasad"
  password = random_password.rds-db-password.result

  publicly_accessible = false

  skip_final_snapshot = true

  # Networking (you MUST already have these)
  db_subnet_group_name   = aws_db_subnet_group.tf-rds-subnet-group.name
  vpc_security_group_ids = [aws_security_group.db_sg.id]
}