# Create ALB
resource "aws_lb" "tf-ecs-awslens-alb" {
  name               = "${var.prefix}-${var.app_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg.id]
  subnets            = module.network.public_subnet_ids

  enable_deletion_protection = false

}

# Create target group
resource "aws_lb_target_group" "tf-ecs-awslens-tg" {
  name        = "${var.prefix}-${var.app_name}-tg"
  port        = 5000
  protocol    = "HTTP"
  vpc_id      = module.network.vpc_id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/health"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = "200"
  }
}


# Create listener
/* resource "aws_lb_listener" "tf-ecs-awslens-listener" {
  load_balancer_arn = aws_lb.tf-ecs-awslens-alb.arn
  port              = "443"
  protocol          = "HTTPS"
  certificate_arn   = aws_acm_certificate.cert.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tf-ecs-awslens-tg.arn
  }
} */