#!/bin/bash
# poc8 — AWS infrastructure provisioning
#
# HOW TO USE: don't blindly execute this end to end. Run it section by
# section (copy-paste into your terminal, or `bash -x` one function at a
# time), checking each printed resource ID before moving to the next
# section — several later steps depend on IDs captured earlier, and a
# couple of steps (ACM validation, DNS) require you to wait on external
# propagation before continuing.
#
# Prereqs: AWS CLI v2 configured with your admin credentials
# (`aws configure` or `aws sso login`), Docker Desktop running, and
# `jq` installed (`brew install jq`).

set -euo pipefail

# ============================================================================
# 0. VARIABLES — edit these first
# ============================================================================
export AWS_REGION="ap-south-1"           # Mumbai — matches your new default VPC
export PROJECT="poc8"
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# Reusing your existing default VPC — no new VPC/IGW/NAT to create.
# All 3 default subnets share one route table with a route to the IGW,
# so any of them work; picking 2 in different AZs for the ALB (a requirement)
# and one of those for the EC2 instance.
export VPC_ID="vpc-0ec5746af9c8e487d"
export ALB_SUBNET_1="subnet-0d0e539ce76673644"   # ap-south-1a
export ALB_SUBNET_2="subnet-0440cfc4947f73ad9"   # ap-south-1b
export EC2_SUBNET_1="subnet-0d0e539ce76673644"   # ap-south-1a — instance lands here
export EC2_SUBNET_2="subnet-0440cfc4947f73ad9"   # ap-south-1b — ASG can also place a replacement here
# subnet-0ceb69754ad5524ec (ap-south-1c) exists but isn't used here — fine
# to add later if you want a third AZ behind the ALB.

export INSTANCE_TYPE="t4g.large"        # Graviton (ARM64) — cheaper, matches Apple Silicon builds
export APP_PORT="8000"
export DOMAIN_NAME=""                   # e.g. poc8.yourcompany.com — leave blank to skip ACM/Route53 for now
export HOSTED_ZONE_ID=""                # only if the domain is hosted in Route 53

echo "Account: ${ACCOUNT_ID}  Region: ${AWS_REGION}  VPC: ${VPC_ID}"

# ============================================================================
# 1. VERIFY the subnets we're using auto-assign public IPs
#    (default-VPC subnets normally do; this just makes sure)
# ============================================================================
aws ec2 modify-subnet-attribute --subnet-id "${ALB_SUBNET_1}" --map-public-ip-on-launch
aws ec2 modify-subnet-attribute --subnet-id "${ALB_SUBNET_2}" --map-public-ip-on-launch
echo "Confirmed auto-assign public IP on ${ALB_SUBNET_1}, ${ALB_SUBNET_2}"

# No VPC, IGW, or NAT Gateway creation needed — this VPC already has an IGW
# (igw-016d1775b364cad95) and every subnet already routes 0.0.0.0/0 to it.
# The EC2 instance will get its own public IP and reach the internet directly
# through that existing IGW route; the security group below is what actually
# keeps it locked down (only the ALB can reach the app port; no SSH inbound).

# ============================================================================
# 2. SECURITY GROUPS
# ============================================================================
ALB_SG=$(aws ec2 create-security-group --group-name "${PROJECT}-alb-sg" \
  --description "ALB inbound from internet" --vpc-id "${VPC_ID}" --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "${ALB_SG}" --protocol tcp --port 80 --cidr 0.0.0.0/0
aws ec2 authorize-security-group-ingress --group-id "${ALB_SG}" --protocol tcp --port 443 --cidr 0.0.0.0/0

EC2_SG=$(aws ec2 create-security-group --group-name "${PROJECT}-ec2-sg" \
  --description "App instance, inbound from ALB only" --vpc-id "${VPC_ID}" --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id "${EC2_SG}" --protocol tcp --port "${APP_PORT}" \
  --source-group "${ALB_SG}"
# No inbound SSH rule — use SSM Session Manager instead of opening port 22.
echo "ALB SG: ${ALB_SG}  EC2 SG: ${EC2_SG}"

# ============================================================================
# 3. ECR — build & push image (run this from your Mac, in the poc8 code dir)
# ============================================================================
aws ecr create-repository --repository-name "${PROJECT}" --region "${AWS_REGION}" > /dev/null || true
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT}"
echo "ECR repo: ${ECR_URI}"
echo ""
echo ">>> Now, from your local poc8 code directory (with the Dockerfile from this guide):"
echo "    aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "    docker buildx build --platform linux/arm64 -t ${ECR_URI}:latest --push ."
echo ""
read -p "Press enter once the image has been pushed to continue..."

# ============================================================================
# 4. IAM ROLE for the EC2 instance (Bedrock, ECR pull, S3 backup, SSM, logs)
# ============================================================================
BACKUP_BUCKET="${PROJECT}-backups-${ACCOUNT_ID}"
# us-east-1 is the one region where --create-bucket-configuration must be
# omitted entirely (the API rejects an explicit LocationConstraint for it).
if [ "${AWS_REGION}" = "us-east-1" ]; then
  aws s3api create-bucket --bucket "${BACKUP_BUCKET}" --region "${AWS_REGION}" 2>/dev/null || true
else
  aws s3api create-bucket --bucket "${BACKUP_BUCKET}" --region "${AWS_REGION}" \
    --create-bucket-configuration LocationConstraint="${AWS_REGION}" 2>/dev/null || true
fi
aws s3api put-bucket-encryption --bucket "${BACKUP_BUCKET}" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "${BACKUP_BUCKET}" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

cat > /tmp/${PROJECT}-trust-policy.json <<'EOF'
{ "Version": "2012-10-17", "Statement": [{ "Effect": "Allow",
  "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole" }] }
EOF
aws iam create-role --role-name "${PROJECT}-ec2-role" \
  --assume-role-policy-document file:///tmp/${PROJECT}-trust-policy.json > /dev/null

aws iam attach-role-policy --role-name "${PROJECT}-ec2-role" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name "${PROJECT}-ec2-role" \
  --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
aws iam attach-role-policy --role-name "${PROJECT}-ec2-role" \
  --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy

cat > /tmp/${PROJECT}-inline-policy.json <<EOF
{ "Version": "2012-10-17", "Statement": [
  { "Sid": "InvokeGlobalCrossRegionInferenceProfile",
    "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": [
      "arn:aws:bedrock:${AWS_REGION}:${ACCOUNT_ID}:inference-profile/*",
      "arn:aws:bedrock:${AWS_REGION}::inference-profile/*"
    ] },
  { "Sid": "InvokeUnderlyingFoundationModels",
    "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "arn:aws:bedrock:*::foundation-model/*" },
  { "Effect": "Allow", "Action": ["s3:GetObject","s3:PutObject","s3:ListBucket"],
    "Resource": ["arn:aws:s3:::${BACKUP_BUCKET}", "arn:aws:s3:::${BACKUP_BUCKET}/*"] }
] }
EOF
aws iam put-role-policy --role-name "${PROJECT}-ec2-role" \
  --policy-name "${PROJECT}-app-permissions" --policy-document file:///tmp/${PROJECT}-inline-policy.json

aws iam create-instance-profile --instance-profile-name "${PROJECT}-instance-profile" > /dev/null
aws iam add-role-to-instance-profile --instance-profile-name "${PROJECT}-instance-profile" \
  --role-name "${PROJECT}-ec2-role"
echo "IAM role + instance profile created. Backup bucket: ${BACKUP_BUCKET}"
sleep 10  # let IAM propagate before referencing the instance profile below

# ============================================================================
# 5. LAUNCH TEMPLATE
# ============================================================================
AMI_ID=$(aws ssm get-parameter \
  --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query 'Parameter.Value' --output text --region "${AWS_REGION}")

sed -e "s#__ECR_IMAGE_URI__#${ECR_URI}:latest#" \
    -e "s#__BACKUP_BUCKET__#${BACKUP_BUCKET}#" \
    -e "s#__AWS_REGION__#${AWS_REGION}#" \
    user-data.sh > /tmp/${PROJECT}-user-data-final.sh
USER_DATA_B64=$(base64 < /tmp/${PROJECT}-user-data-final.sh | tr -d '\n')

cat > /tmp/${PROJECT}-lt.json <<EOF
{
  "ImageId": "${AMI_ID}",
  "InstanceType": "${INSTANCE_TYPE}",
  "IamInstanceProfile": {"Name": "${PROJECT}-instance-profile"},
  "SecurityGroupIds": ["${EC2_SG}"],
  "UserData": "${USER_DATA_B64}",
  "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 50, "VolumeType": "gp3", "DeleteOnTermination": true}}],
  "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "${PROJECT}-app"}]}]
}
EOF
aws ec2 create-launch-template --launch-template-name "${PROJECT}-lt" \
  --launch-template-data file:///tmp/${PROJECT}-lt.json > /dev/null
echo "Launch template ${PROJECT}-lt created (AMI ${AMI_ID})"

# ============================================================================
# 6. TARGET GROUP + ALB
# ============================================================================
TG_ARN=$(aws elbv2 create-target-group --name "${PROJECT}-tg" --protocol HTTP --port "${APP_PORT}" \
  --vpc-id "${VPC_ID}" --target-type instance \
  --health-check-path /health --health-check-interval-seconds 15 \
  --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)

ALB_ARN=$(aws elbv2 create-load-balancer --name "${PROJECT}-alb" --type application --scheme internet-facing \
  --subnets "${ALB_SUBNET_1}" "${ALB_SUBNET_2}" --security-groups "${ALB_SG}" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)
ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "${ALB_ARN}" \
  --query 'LoadBalancers[0].DNSName' --output text)
echo "ALB: ${ALB_DNS}"

# Default ALB idle timeout is 60s. The app's Bedrock calls (schema discovery,
# YAML generation, NL-to-SQL) run synchronously and can legitimately take
# minutes with max_tokens=50000, sending nothing back to the client until
# the whole thing finishes — well past 60s. Match gunicorn's --timeout 420
# (Dockerfile) so neither one kills the connection while the backend is
# still genuinely working.
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "${ALB_ARN}" \
  --attributes Key=idle_timeout.timeout_seconds,Value=420 > /dev/null
echo "ALB idle timeout set to 420s (matches gunicorn --timeout)"

if [ -n "${DOMAIN_NAME}" ]; then
  CERT_ARN=$(aws acm request-certificate --domain-name "${DOMAIN_NAME}" --validation-method DNS \
    --region "${AWS_REGION}" --query 'CertificateArn' --output text)
  echo "ACM certificate requested: ${CERT_ARN}"
  echo "Add the DNS validation CNAME record ACM gives you, then wait for validation before continuing:"
  echo "    aws acm describe-certificate --certificate-arn ${CERT_ARN} --query 'Certificate.DomainValidationOptions'"
  read -p "Press enter once the certificate status is ISSUED..."

  aws elbv2 create-listener --load-balancer-arn "${ALB_ARN}" --protocol HTTPS --port 443 \
    --certificates CertificateArn="${CERT_ARN}" \
    --default-actions Type=forward,TargetGroupArn="${TG_ARN}" > /dev/null
  aws elbv2 create-listener --load-balancer-arn "${ALB_ARN}" --protocol HTTP --port 80 \
    --default-actions "Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,StatusCode=HTTP_301}" > /dev/null

  if [ -n "${HOSTED_ZONE_ID}" ]; then
    ALB_ZONE_ID=$(aws elbv2 describe-load-balancers --load-balancer-arns "${ALB_ARN}" \
      --query 'LoadBalancers[0].CanonicalHostedZoneId' --output text)
    cat > /tmp/${PROJECT}-r53.json <<EOF
{ "Changes": [{ "Action": "UPSERT", "ResourceRecordSet": {
  "Name": "${DOMAIN_NAME}", "Type": "A",
  "AliasTarget": {"HostedZoneId": "${ALB_ZONE_ID}", "DNSName": "${ALB_DNS}", "EvaluateTargetHealth": true} } }] }
EOF
    aws route53 change-resource-record-sets --hosted-zone-id "${HOSTED_ZONE_ID}" \
      --change-batch file:///tmp/${PROJECT}-r53.json
    echo "Route 53 alias record created for ${DOMAIN_NAME}"
  else
    echo "Point your DNS provider's CNAME for ${DOMAIN_NAME} at: ${ALB_DNS}"
  fi
else
  echo "No DOMAIN_NAME set — creating an HTTP-only listener for initial testing."
  echo "Add HTTPS (ACM cert) before sharing the login URL publicly."
  aws elbv2 create-listener --load-balancer-arn "${ALB_ARN}" --protocol HTTP --port 80 \
    --default-actions Type=forward,TargetGroupArn="${TG_ARN}" > /dev/null
fi

# ============================================================================
# 7. AUTO SCALING GROUP (min=max=desired=1 — see guide for why, given DuckDB)
# ============================================================================
aws autoscaling create-auto-scaling-group --auto-scaling-group-name "${PROJECT}-asg" \
  --launch-template "LaunchTemplateName=${PROJECT}-lt,Version=\$Latest" \
  --min-size 1 --max-size 1 --desired-capacity 1 \
  --vpc-zone-identifier "${EC2_SUBNET_1},${EC2_SUBNET_2}" \
  --target-group-arns "${TG_ARN}" \
  --health-check-type ELB --health-check-grace-period 120 \
  --tags "Key=Name,Value=${PROJECT}-app,PropagateAtLaunch=true"
echo "ASG created — instance will boot, pull the image, and register with the target group."

echo ""
echo "=================================================================="
echo "Done. Test with: curl http://${ALB_DNS}/health  (or https:// once cert is attached)"
echo "Resource IDs — save these:"
echo "  VPC=${VPC_ID}  ALB=${ALB_DNS}  ECR=${ECR_URI}  BACKUP_BUCKET=${BACKUP_BUCKET}"
echo "=================================================================="
