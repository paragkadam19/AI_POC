#!/bin/bash
# EC2 user-data (Amazon Linux 2023). Runs once at first boot as root.
# Fill in the four variables below before base64-encoding this into the
# launch template (deploy.sh does this for you).

set -euo pipefail

AWS_REGION="__AWS_REGION__"                # e.g. us-east-1
ECR_IMAGE_URI="__ECR_IMAGE_URI__"          # e.g. 111122223333.dkr.ecr.us-east-1.amazonaws.com/poc8:latest
BACKUP_BUCKET="__BACKUP_BUCKET__"          # e.g. poc8-backups-111122223333
APP_PORT="8000"

# --- Docker ---------------------------------------------------------------
dnf install -y docker
systemctl enable --now docker
usermod -aG docker ec2-user

# docker compose plugin
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# --- Persistent data dir (lives on the instance's EBS volume) -------------
# Must be /app/storage on the container side — see the Dockerfile comment.
mkdir -p /opt/poc8/storage
chown -R ec2-user:ec2-user /opt/poc8

# Restore last backup from S3 if one exists, so replacing the instance
# (ASG self-healing) doesn't start from an empty DuckDB file.
aws s3 sync "s3://${BACKUP_BUCKET}/storage/" /opt/poc8/storage/ --region "${AWS_REGION}" || true

# --- Pull & run the app -----------------------------------------------------
aws ecr get-login-password --region "${AWS_REGION}" \
    | docker login --username AWS --password-stdin "$(echo "${ECR_IMAGE_URI}" | cut -d/ -f1)"

docker pull "${ECR_IMAGE_URI}"

docker run -d \
    --name poc8-app \
    --restart always \
    -p "${APP_PORT}:${APP_PORT}" \
    -v /opt/poc8/storage:/app/storage \
    -e AWS_REGION="${AWS_REGION}" \
    --log-driver awslogs \
    --log-opt awslogs-region="${AWS_REGION}" \
    --log-opt awslogs-group=/poc8/app \
    --log-opt awslogs-create-group=true \
    --log-opt awslogs-stream="{{.Name}}" \
    "${ECR_IMAGE_URI}"
    # Deliberately NOT setting DISABLE_SSL_VERIFICATION here — that's a
    # local-dev-only workaround for the office network's proxy and should
    # stay off in AWS. See main.py for details.
    #
    # --log-driver awslogs sends the container's stdout/stderr straight to
    # CloudWatch Logs (log group /poc8/app) — this is what actually makes
    # application logs visible, as opposed to the CloudWatch agent config
    # below, which only tails /var/log/messages (host/system logs). Only
    # covers what the app actually writes to stdout/stderr, though — if
    # logger_config.py writes to a file instead, those lines still won't
    # show up here. CloudWatchAgentServerPolicy (already attached to the
    # instance role) includes the logs:* permissions this driver needs, so
    # no separate IAM change was required for this.

# --- Periodic backup to S3 (every 15 min) so instance replacement never
#     loses more than 15 minutes of demo data ---------------------------
cat > /opt/poc8/backup.sh <<EOF
#!/bin/bash
aws s3 sync /opt/poc8/storage/ s3://${BACKUP_BUCKET}/storage/ --region ${AWS_REGION}
EOF
chmod +x /opt/poc8/backup.sh
echo "*/15 * * * * root /opt/poc8/backup.sh >> /var/log/poc8-backup.log 2>&1" > /etc/cron.d/poc8-backup

# --- CloudWatch agent (basic host + Docker log shipping) -------------------
dnf install -y amazon-cloudwatch-agent
# Minimal config: ship syslog + docker logs; adjust as needed.
cat > /opt/aws/amazon-cloudwatch-agent/etc/config.json <<'EOF'
{
  "logs": {
    "logs_collected": {
      "files": {
        "collectors": [
          { "file_path": "/var/log/messages", "log_group_name": "/poc8/ec2/system", "log_stream_name": "{instance_id}" }
        ]
      }
    }
  }
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/config.json