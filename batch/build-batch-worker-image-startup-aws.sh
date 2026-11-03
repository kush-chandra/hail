#!/bin/bash

set -exo pipefail

# The nvidia toolkit must be installed in this startup script to be able to configure docker with the command nvidia-ctk runtime configure --runtime=docker. This command cannot be run from Dockerfile.worker
# The toolkit also has to be installed in Dockerfile.worker since to execute the nvidia hook, the toolkit needs to be installed in the container from which crun is invoked.

echo "=== Installing base packages ==="
apt-get update

apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    jq \
    software-properties-common \
    xfsprogs \
    mdadm

echo "=== Adding Docker apt repo ==="
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | apt-key add -

add-apt-repository \
   "deb [arch=amd64] https://download.docker.com/linux/ubuntu \
   $(lsb_release -cs) \
   stable"

echo "=== Installing Docker ==="
apt-get install -y docker-ce

rm -rf /var/lib/apt/lists/*

[ -f /etc/docker/daemon.json ] || echo "{}" > /etc/docker/daemon.json

echo "=== Adding NVIDIA container toolkit apt repo ==="
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
     | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "=== Installing build tools and kernel headers ==="
apt-get update
apt-get install -y build-essential linux-headers-$(uname -r)

echo "=== Installing amazon-ecr-credential-helper ==="
apt-get install -y amazon-ecr-credential-helper

echo "=== Downloading NVIDIA driver ==="
curl -fsSLO https://us.download.nvidia.com/XFree86/Linux-x86_64/595.91.07/NVIDIA-Linux-x86_64-595.91.07.run
echo "ca23c88dd24b07a191644e1e11cfb7bcdd7537305749af40f980018b095e6313  NVIDIA-Linux-x86_64-595.91.07.run" | sha256sum -c
chmod +x NVIDIA-Linux-x86_64-595.91.07.run

echo "=== Running NVIDIA driver installer ==="
touch /var/log/nvidia-installer.log
tail -f /var/log/nvidia-installer.log &
NVIDIA_LOG_PID=$!
./NVIDIA-Linux-x86_64-595.91.07.run --silent
kill $NVIDIA_LOG_PID

echo "=== Installing NVIDIA container toolkit ==="
apt-get --yes install nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker

echo "=== Installing Amazon CloudWatch Agent ==="
curl -fsSLO https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
dpkg -i amazon-cloudwatch-agent.deb
rm amazon-cloudwatch-agent.deb

echo "=== Configuring Docker with ECR credentials and pulling root image ==="
# avoid "unable to get current user home directory: os/user lookup failed"
export HOME=/root

IMDS_TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
AWS_ACCOUNT_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" http://169.254.169.254/latest/dynamic/instance-identity/document | jq -r .accountId)
# aws_region is always us-east-1
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com"

mkdir -p /root/.docker
[ -f /root/.docker/config.json ] || echo "{}" > /root/.docker/config.json
jq --arg registry "${ECR_REGISTRY}" '.credHelpers[$registry] = "ecr-login"' /root/.docker/config.json > config.json.tmp
mv config.json.tmp /root/.docker/config.json

docker pull {{ global.docker_root_image }}

echo "=== Disabling algif_aead kernel module (copy.fail / CVE-2026-31431) ==="
echo "install algif_aead /bin/false" > /etc/modprobe.d/disable-algif.conf
rmmod algif_aead 2>/dev/null || true

echo "=== Blacklisting ESP modules (copy.fail2) ==="
cat > /etc/modprobe.d/blocklist-esp.conf << 'EOF'
blacklist esp4
blacklist esp6
EOF

echo "=== Enabling Docker debug logging ==="
jq '.debug = true' /etc/docker/daemon.json > daemon.json.tmp
mv daemon.json.tmp /etc/docker/daemon.json

systemctl restart docker

echo "=== Cleaning cloud-init state so workers rerun cloud-init on first boot ==="
cloud-init clean --logs

echo "=== Startup complete, shutting down ==="
shutdown -h now
