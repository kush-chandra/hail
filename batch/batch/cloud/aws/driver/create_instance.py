import base64
import gzip
import json
import logging
import os
from shlex import quote as shq
from typing import Any, Dict, List

from gear.cloud_config import get_global_config
from hailtop.config import get_deploy_config

from ....batch_configuration import DEFAULT_NAMESPACE, DOCKER_PREFIX, DOCKER_ROOT_IMAGE, INTERNAL_GATEWAY_IP
from ....file_store import FileStore
from ....instance_config import InstanceConfig
from ...resource_utils import unreserved_worker_data_disk_size_gib
from ...utils import ACCEPTABLE_QUERY_JAR_URL_PREFIX
from ..resource_utils import aws_machine_type_to_parts

log = logging.getLogger('create_instance')

BATCH_WORKER_IMAGE = os.environ['HAIL_BATCH_WORKER_IMAGE']

# EC2 rejects user data larger than 16 KB before base64 encoding
# https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/user-data.html
MAX_USER_DATA_BYTES = 16 * 1024

ROLE_TAG = 'hail-batch-worker'
NAMESPACE_TAG = 'hail-namespace'


log.info(f'BATCH_WORKER_IMAGE {BATCH_WORKER_IMAGE}')
log.info(f'ACCEPTABLE_QUERY_JAR_URL_PREFIX {ACCEPTABLE_QUERY_JAR_URL_PREFIX}')


def create_run_script(
    file_store: FileStore,
    region: str,
    machine_name: str,
    activation_token: str,
    max_idle_time_msecs: int,
    unreserved_disk_storage_gb: int,
    instance_config: InstanceConfig,
) -> str:
    make_global_config = ['mkdir /global-config']
    global_config = get_global_config()
    for name, value in global_config.items():
        make_global_config.append(f'echo -n {shq(value)} > /global-config/{name}')
    make_global_config_str = '\n'.join(make_global_config)

    instance_config_b64 = base64.b64encode(json.dumps(instance_config.to_dict()).encode()).decode()

    return rf"""#!/bin/bash
# cloud-init waits for user data scripts to exit, so run the worker detached
if [ -z "$HAIL_DETACHED" ]; then
    HAIL_DETACHED=1 nohup setsid /bin/bash "$0" >/run.log 2>&1 </dev/null &
    exit 0
fi

set -x

NAME={shq(machine_name)}
NAMESPACE={shq(DEFAULT_NAMESPACE)}
ACTIVATION_TOKEN={shq(activation_token)}
REGION={shq(region)}
BATCH_LOGS_STORAGE_URI={shq(file_store.batch_logs_storage_uri)}
INSTANCE_ID={shq(file_store.instance_id)}
INSTANCE_CONFIG={shq(instance_config_b64)}
MAX_IDLE_TIME_MSECS={shq(str(max_idle_time_msecs))}
BATCH_WORKER_IMAGE={shq(BATCH_WORKER_IMAGE)}
DOCKER_ROOT_IMAGE={shq(DOCKER_ROOT_IMAGE)}
DOCKER_PREFIX={shq(DOCKER_PREFIX)}
INTERNAL_GATEWAY_IP={shq(INTERNAL_GATEWAY_IP)}
UNRESERVED_WORKER_DATA_DISK_SIZE_GB={shq(str(unreserved_disk_storage_gb))}
ACCEPTABLE_QUERY_JAR_URL_PREFIX={shq(ACCEPTABLE_QUERY_JAR_URL_PREFIX)}

CORES=$(nproc)

IMDS_TOKEN=$(curl -fsS -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
imds() {{
    curl -fsS -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "http://169.254.169.254/latest/meta-data/$1"
}}
IP_ADDRESS=$(imds local-ipv4)
ZONE=$(imds placement/availability-zone)
MAC=$(imds mac)
VPC_CIDR=$(imds network/interfaces/macs/$MAC/vpc-ipv4-cidr-block)

# Setup the CloudWatch agent before anything else so startup failures are visible in CloudWatch Logs
touch /worker.log

cat >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<EOF
{{
  "agent": {{"run_as_user": "root"}},
  "logs": {{
    "logs_collected": {{
      "files": {{
        "collect_list": [
          {{"file_path": "/run.log", "log_group_name": "batch-worker-$NAMESPACE", "log_stream_name": "$NAME/run.log", "retention_in_days": 30}},
          {{"file_path": "/worker.log", "log_group_name": "batch-worker-$NAMESPACE", "log_stream_name": "$NAME/worker.log", "retention_in_days": 30}},
          {{"file_path": "/batch/jvm-container-logs/jvm-*.log", "log_group_name": "batch-worker-$NAMESPACE", "log_stream_name": "$NAME/jvm.log", "retention_in_days": 30}}
        ]
      }}
    }}
  }}
}}
EOF

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# The data disk is the only EBS volume besides the root volume. NVMe device names are not
# stable across boots, so find the disk that does not hold the root filesystem.
ROOT_DISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
for i in $(seq 1 60); do
    WORKER_DATA_DISK_NAME=$(lsblk -dno NAME,TYPE | awk -v root="$ROOT_DISK" '$2 == "disk" && $1 != root {{ print $1; exit }}')
    [ -n "$WORKER_DATA_DISK_NAME" ] && break
    sleep 1
done
if [ -z "$WORKER_DATA_DISK_NAME" ]; then
    echo "could not find the worker data disk"
    lsblk
    shutdown -h now
    exit 1
fi

# format worker data disk
mkfs.xfs -m reflink=1 -n ftype=1 /dev/$WORKER_DATA_DISK_NAME
mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME
mount -o prjquota /dev/$WORKER_DATA_DISK_NAME /mnt/disks/$WORKER_DATA_DISK_NAME
chmod a+w /mnt/disks/$WORKER_DATA_DISK_NAME
XFS_DEVICE=$(xfs_info /mnt/disks/$WORKER_DATA_DISK_NAME | head -n 1 | awk '{{ print $1 }}' | awk  'BEGIN {{ FS = "=" }}; {{ print $2 }}')

# reconfigure docker and containerd to use the data disk
service docker stop
service containerd stop
mv /var/lib/docker /mnt/disks/$WORKER_DATA_DISK_NAME/docker
ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/docker /var/lib/docker
mv /var/lib/containerd /mnt/disks/$WORKER_DATA_DISK_NAME/containerd
ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/containerd /var/lib/containerd
service containerd start
service docker start

# reconfigure /batch and /logs and /cloudfuse to use the data disk
mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/batch/
ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/batch /batch

mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/logs/
ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/logs /logs

mkdir -p /mnt/disks/$WORKER_DATA_DISK_NAME/cloudfuse/
ln -s /mnt/disks/$WORKER_DATA_DISK_NAME/cloudfuse /cloudfuse

mkdir -p /etc/netns

mkdir -p /batch/jvm-container-logs/

# private job network = 172.20.0.0/16
# public job network = 172.21.0.0/16
# [all networks] Rewrite traffic coming from containers to masquerade as the host
iptables --table nat --append POSTROUTING --source 172.20.0.0/15 --jump MASQUERADE

# [all networks] Jobs must not reach the instance metadata service, which holds this
# worker's credentials and user data
iptables --insert FORWARD --source 172.20.0.0/15 --destination 169.254.169.254 --jump DROP

# [public]
# Allow the internal gateway
iptables --append FORWARD --destination $INTERNAL_GATEWAY_IP --jump ACCEPT
# And this worker
iptables --append FORWARD --destination $IP_ADDRESS --jump ACCEPT
# Allow traffic going to the internet
INTERNET_INTERFACE=$(ip route show default | awk '{{ print $5; exit }}')
iptables --append FORWARD --out-interface $INTERNET_INTERFACE ! --destination $VPC_CIDR --jump ACCEPT

# [private]
# Allow all traffic from the private job network
iptables --append FORWARD --source 172.20.0.0/16 --jump ACCEPT

{make_global_config_str}

mkdir /deploy-config
cat >/deploy-config/deploy-config.json <<EOF
{json.dumps(get_deploy_config().with_location('gce').get_config())}
EOF

# docker authenticates to ECR with the amazon-ecr-credential-helper baked into the worker image
# retry once
docker pull $BATCH_WORKER_IMAGE || \
(echo 'pull failed, retrying' && sleep 15 && docker pull $BATCH_WORKER_IMAGE)

BATCH_WORKER_IMAGE_ID=$(docker inspect $BATCH_WORKER_IMAGE --format='{{{{.Id}}}}' | cut -d':' -f2)

docker run \
--name worker \
-e CLOUD=aws \
-e CORES=$CORES \
-e NAME=$NAME \
-e NAMESPACE=$NAMESPACE \
-e ACTIVATION_TOKEN=$ACTIVATION_TOKEN \
-e IP_ADDRESS=$IP_ADDRESS \
-e BATCH_LOGS_STORAGE_URI=$BATCH_LOGS_STORAGE_URI \
-e INSTANCE_ID=$INSTANCE_ID \
-e ZONE=$ZONE \
-e REGION=$REGION \
-e AWS_REGION=$REGION \
-e DOCKER_PREFIX=$DOCKER_PREFIX \
-e DOCKER_ROOT_IMAGE=$DOCKER_ROOT_IMAGE \
-e INSTANCE_CONFIG=$INSTANCE_CONFIG \
-e MAX_IDLE_TIME_MSECS=$MAX_IDLE_TIME_MSECS \
-e BATCH_WORKER_IMAGE=$BATCH_WORKER_IMAGE \
-e BATCH_WORKER_IMAGE_ID=$BATCH_WORKER_IMAGE_ID \
-e INTERNET_INTERFACE=$INTERNET_INTERFACE \
-e UNRESERVED_WORKER_DATA_DISK_SIZE_GB=$UNRESERVED_WORKER_DATA_DISK_SIZE_GB \
-e ACCEPTABLE_QUERY_JAR_URL_PREFIX=$ACCEPTABLE_QUERY_JAR_URL_PREFIX \
-e INTERNAL_GATEWAY_IP=$INTERNAL_GATEWAY_IP \
-v /var/run/docker.sock:/var/run/docker.sock \
-v /var/run/netns:/var/run/netns:shared \
-v /usr/bin/docker:/usr/bin/docker \
-v /batch:/batch:shared \
-v /logs:/logs \
-v /global-config:/global-config \
-v /deploy-config:/deploy-config \
-v /cloudfuse:/cloudfuse:shared \
-v /etc/netns:/etc/netns \
-v /sys/fs/cgroup:/sys/fs/cgroup \
--mount type=bind,source=/mnt/disks/$WORKER_DATA_DISK_NAME,target=/host \
--mount type=bind,source=/dev,target=/dev,bind-propagation=rshared \
--device /dev/fuse \
--device $XFS_DEVICE \
--device /dev \
--privileged \
--cap-add SYS_ADMIN \
--security-opt apparmor:unconfined \
--network host \
--cgroupns host \
$BATCH_WORKER_IMAGE \
python3 -u -m batch.worker.worker >>/worker.log 2>&1

[ $? -eq 0 ] || tail -n 1000 /worker.log

journalctl -u docker.service > /logs/dockerd.log

# the instance is launched with InstanceInitiatedShutdownBehavior=terminate
shutdown -h now
"""


def create_user_data(run_script: str) -> bytes:
    # cloud-init transparently decompresses gzipped user data
    user_data = gzip.compress(run_script.encode(), mtime=0)
    assert len(user_data) <= MAX_USER_DATA_BYTES, len(user_data)
    return user_data


def create_vm_config(
    file_store: FileStore,
    resource_rates: Dict[str, float],
    region: str,
    machine_name: str,
    machine_type: str,
    activation_token: str,
    max_idle_time_msecs: int,
    data_disk_size_gb: int,
    boot_disk_size_gb: int,
    preemptible: bool,
    job_private: bool,
    image_id: str,
    root_device_name: str,
    security_group_id: str,
    instance_profile_name: str,
    instance_config: InstanceConfig,
) -> Dict[str, Any]:
    """Build the RunInstances arguments for a worker, excluding the subnet and client token."""
    parts = aws_machine_type_to_parts(machine_type)
    assert parts
    cores = parts.cores

    if job_private:
        unreserved_disk_storage_gb = data_disk_size_gb
    else:
        unreserved_disk_storage_gb = unreserved_worker_data_disk_size_gib(data_disk_size_gb, cores)
    assert unreserved_disk_storage_gb >= 0

    assert instance_config.is_valid_configuration(resource_rates.keys())

    run_script = create_run_script(
        file_store,
        region,
        machine_name,
        activation_token,
        max_idle_time_msecs,
        unreserved_disk_storage_gb,
        instance_config,
    )

    tags: List[Dict[str, str]] = [
        {'Key': 'Name', 'Value': machine_name},
        {'Key': NAMESPACE_TAG, 'Value': DEFAULT_NAMESPACE},
        {'Key': ROLE_TAG, 'Value': 'true'},
    ]

    config: Dict[str, Any] = {
        'ImageId': image_id,
        'InstanceType': machine_type,
        'SecurityGroupIds': [security_group_id],
        'IamInstanceProfile': {'Name': instance_profile_name},
        'UserData': create_user_data(run_script),
        'InstanceInitiatedShutdownBehavior': 'terminate',
        'BlockDeviceMappings': [
            {
                'DeviceName': root_device_name,
                'Ebs': {'VolumeSize': boot_disk_size_gb, 'VolumeType': 'gp3', 'DeleteOnTermination': True},
            },
            {
                'DeviceName': '/dev/sdb',
                'Ebs': {'VolumeSize': data_disk_size_gb, 'VolumeType': 'gp3', 'DeleteOnTermination': True},
            },
        ],
        'MetadataOptions': {
            'HttpEndpoint': 'enabled',
            'HttpTokens': 'required',
            # containers on bridge networks are an extra hop away and cannot reach the metadata service
            'HttpPutResponseHopLimit': 1,
        },
        'TagSpecifications': [
            {'ResourceType': 'instance', 'Tags': tags},
            {'ResourceType': 'volume', 'Tags': tags},
        ],
    }

    if preemptible:
        config['InstanceMarketOptions'] = {
            'MarketType': 'spot',
            'SpotOptions': {
                'SpotInstanceType': 'one-time',
                'InstanceInterruptionBehavior': 'terminate',
            },
        }

    return config
