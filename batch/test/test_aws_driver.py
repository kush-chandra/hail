import datetime
import gzip
import os
from typing import Any, Dict, List

import botocore.exceptions
import pytest

# batch.batch_configuration and batch.cloud.utils read these at import time
for _name, _value in {
    'HAIL_DEFAULT_NAMESPACE': 'default',
    'HAIL_SCOPE': 'test',
    'CLOUD': 'aws',
    'HAIL_DOCKER_ROOT_IMAGE': '123.dkr.ecr.us-east-1.amazonaws.com/hail/ubuntu:24.04',
    'HAIL_DOCKER_PREFIX': '123.dkr.ecr.us-east-1.amazonaws.com/hail',
    'KUBERNETES_SERVER_URL': 'https://kubernetes.example',
    'INTERNAL_GATEWAY_IP': '10.0.0.10',
    'HAIL_BATCH_STORAGE_URI': 's3://hail-batch',
    'HAIL_SHA': 'deadbeef',
    'HAIL_QUERY_STORAGE_URI': 's3://hail-query',
    'HAIL_QUERY_ACCEPTABLE_JAR_SUBFOLDER': '/jars',
    'HAIL_BATCH_WORKER_IMAGE': '123.dkr.ecr.us-east-1.amazonaws.com/hail/batch-worker:abc',
}.items():
    os.environ.setdefault(_name, _value)

from batch.cloud.aws.driver import create_instance  # noqa: E402
from batch.cloud.aws.driver.billing_manager import AWSBillingManager  # noqa: E402
from batch.cloud.aws.driver.driver import resolve_worker_launch_settings, worker_image_name  # noqa: E402
from batch.cloud.aws.driver.resource_manager import (  # noqa: E402
    AWSResourceManager,
    AWSWorkerLaunchSettings,
    client_token,
    ec2_instance_to_vm_state,
)
from batch.cloud.aws.instance_config import AWSSlimInstanceConfig  # noqa: E402
from batch.cloud.aws.resource_utils import (  # noqa: E402
    aws_machine_type_to_cores_and_memory_bytes,
    aws_machine_type_to_parts,
    aws_valid_machine_types,
    aws_worker_properties_to_machine_type,
)
from batch.cloud.resource_utils import possible_cores_from_worker_type, valid_machine_types  # noqa: E402
from batch.driver.billing_manager import ProductVersions  # noqa: E402
from batch.driver.resource_manager import (  # noqa: E402
    UnknownVMState,
    VMDoesNotExist,
    VMStateCreating,
    VMStateRunning,
    VMStateTerminated,
)
from hailtop.utils import parse_docker_image_reference  # noqa: E402


class FakeFileStore:
    batch_logs_storage_uri = 's3://hail-batch/logs'
    instance_id = 'abc123'


class FakeInstance:
    def __init__(self, name: str, time_created: int = 1000):
        self.name = name
        self.time_created = time_created


def client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError({'Error': {'Code': code, 'Message': code}}, 'RunInstances')


class FakeEc2Client:
    def __init__(self):
        self.run_instances_calls: List[Dict[str, Any]] = []
        self.run_instances_errors: List[str] = []
        self.instances: List[Dict[str, Any]] = []
        self.terminated: List[str] = []
        self.images: List[Dict[str, Any]] = []
        self.subnets: List[Dict[str, Any]] = []
        self.security_groups: List[Dict[str, Any]] = []
        self.filters: Dict[str, Any] = {}

    async def run_instances(self, **kwargs):
        self.run_instances_calls.append(kwargs)
        if self.run_instances_errors:
            raise client_error(self.run_instances_errors.pop(0))
        return {'InstanceId': f'i-{len(self.run_instances_calls)}'}

    async def describe_instances(self, filters):
        self.filters['instances'] = filters
        return self.instances

    async def terminate_instances(self, instance_ids):
        self.terminated.extend(instance_ids)

    async def describe_images(self, owners, filters):
        self.filters['images'] = (owners, filters)
        return self.images

    async def describe_subnets(self, filters):
        self.filters['subnets'] = filters
        return self.subnets

    async def describe_security_groups(self, filters):
        self.filters['security_groups'] = filters
        return self.security_groups


@pytest.fixture
def global_config(monkeypatch):
    monkeypatch.setattr(create_instance, 'get_global_config', lambda: {'cloud': 'aws', 'aws_region': 'us-east-1'})


def launch_settings(subnet_ids=('subnet-a', 'subnet-b', 'subnet-c')) -> AWSWorkerLaunchSettings:
    return AWSWorkerLaunchSettings(
        image_id='ami-123',
        root_device_name='/dev/sda1',
        subnet_ids=list(subnet_ids),
        security_group_id='sg-123',
        instance_profile_name='batch-worker',
    )


def instance_config(machine_type='m6i.4xlarge', preemptible=True, job_private=False) -> AWSSlimInstanceConfig:
    return AWSSlimInstanceConfig.create(
        product_versions=ProductVersions({}),
        machine_type=machine_type,
        preemptible=preemptible,
        local_ssd_data_disk=False,
        data_disk_size_gb=375,
        boot_disk_size_gb=30,
        job_private=job_private,
        location='us-east-1',
    )


def test_worker_type_and_cores_to_machine_type():
    assert aws_worker_properties_to_machine_type('standard', 16) == 'm6i.4xlarge'
    assert aws_worker_properties_to_machine_type('highmem', 2) == 'r6i.large'
    assert aws_worker_properties_to_machine_type('highcpu', 128) == 'c6i.32xlarge'

    assert aws_machine_type_to_cores_and_memory_bytes('m6i.4xlarge') == (16, 64 * 1024**3)
    assert aws_machine_type_to_cores_and_memory_bytes('r6i.large') == (2, 16 * 1024**3)
    assert aws_machine_type_to_cores_and_memory_bytes('c6i.xlarge') == (4, 8 * 1024**3)

    assert aws_machine_type_to_parts('m7i.4xlarge') is None
    assert aws_machine_type_to_parts('m6i.metal') is None
    assert aws_machine_type_to_parts('n2-standard-16') is None

    for worker_type in ('standard', 'highmem', 'highcpu'):
        for cores in possible_cores_from_worker_type('aws', worker_type):
            machine_type = aws_worker_properties_to_machine_type(worker_type, cores)
            parts = aws_machine_type_to_parts(machine_type)
            assert parts is not None
            assert (parts.worker_type, parts.cores) == (worker_type, cores)
            assert machine_type in valid_machine_types('aws')
    assert len(aws_valid_machine_types) == 27


def test_instance_config_round_trip():
    config = instance_config(machine_type='r6i.2xlarge', preemptible=False, job_private=True)
    assert config.cloud == 'aws'
    assert config.worker_type() == 'highmem'
    assert config.cores == 8
    assert config.instance_memory() == 64 * 1024**3
    assert config.region_for('us-east-1') == 'us-east-1'

    # billing is not implemented yet, so instances cost nothing
    assert config.quantified_resources(cpu_in_mcpu=8000, memory_in_bytes=64 * 1024**3, extra_storage_in_gib=0) == []
    assert config.is_valid_configuration([])
    assert config.cost_per_hour({}) == 0

    d = config.to_dict()
    assert d['cloud'] == 'aws'
    round_tripped = AWSSlimInstanceConfig.from_dict(d)
    assert round_tripped.to_dict() == d


@pytest.mark.parametrize(
    'state,expected',
    [
        ('pending', VMStateCreating),
        ('running', VMStateRunning),
        ('shutting-down', VMStateTerminated),
        ('terminated', VMStateTerminated),
        ('stopping', VMStateTerminated),
        ('stopped', VMStateTerminated),
        ('rebooting', UnknownVMState),
    ],
)
def test_ec2_state_to_vm_state(state, expected):
    launch_time = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    vm_state = ec2_instance_to_vm_state(
        {'InstanceId': 'i-1', 'State': {'Name': state}, 'LaunchTime': launch_time}, time_created=1000
    )
    assert type(vm_state) is expected
    if expected is VMStateRunning:
        assert vm_state.last_state_change_timestamp_msecs == int(launch_time.timestamp() * 1000)
    if expected is VMStateCreating:
        assert vm_state.last_state_change_timestamp_msecs == 1000


def vm_config(preemptible: bool, job_private: bool = False) -> Dict[str, Any]:
    return create_instance.create_vm_config(
        FakeFileStore(),  # type: ignore
        {},
        'us-east-1',
        'batch-worker-default-standard-abcde',
        'm6i.4xlarge',
        'activation-token',
        60_000,
        375,
        30,
        preemptible,
        job_private,
        'ami-123',
        '/dev/sda1',
        'sg-123',
        'batch-worker',
        instance_config(preemptible=preemptible, job_private=job_private),
    )


@pytest.mark.parametrize('preemptible', [True, False])
def test_create_vm_config(global_config, preemptible):
    config = vm_config(preemptible)

    assert config['ImageId'] == 'ami-123'
    assert config['InstanceType'] == 'm6i.4xlarge'
    assert config['SecurityGroupIds'] == ['sg-123']
    assert config['IamInstanceProfile'] == {'Name': 'batch-worker'}
    assert config['InstanceInitiatedShutdownBehavior'] == 'terminate'
    assert 'SubnetId' not in config and 'ClientToken' not in config

    assert config['BlockDeviceMappings'] == [
        {'DeviceName': '/dev/sda1', 'Ebs': {'VolumeSize': 30, 'VolumeType': 'gp3', 'DeleteOnTermination': True}},
        {'DeviceName': '/dev/sdb', 'Ebs': {'VolumeSize': 375, 'VolumeType': 'gp3', 'DeleteOnTermination': True}},
    ]
    assert config['MetadataOptions'] == {
        'HttpEndpoint': 'enabled',
        'HttpTokens': 'required',
        'HttpPutResponseHopLimit': 1,
    }

    for spec in config['TagSpecifications']:
        tags = {tag['Key']: tag['Value'] for tag in spec['Tags']}
        assert tags == {
            'Name': 'batch-worker-default-standard-abcde',
            'hail-namespace': 'default',
            'hail-batch-worker': 'true',
        }

    if preemptible:
        assert config['InstanceMarketOptions'] == {
            'MarketType': 'spot',
            'SpotOptions': {'SpotInstanceType': 'one-time', 'InstanceInterruptionBehavior': 'terminate'},
        }
    else:
        assert 'InstanceMarketOptions' not in config


def test_user_data_fits_and_runs_the_worker(global_config):
    user_data = vm_config(preemptible=True)['UserData']
    assert len(user_data) <= create_instance.MAX_USER_DATA_BYTES

    script = gzip.decompress(user_data).decode()
    assert script.startswith('#!/bin/bash\n')
    assert "ACTIVATION_TOKEN=activation-token" in script
    assert "NAME=batch-worker-default-standard-abcde" in script
    assert '-e CLOUD=aws' in script
    assert 'python3 -u -m batch.worker.worker' in script
    assert 'shutdown -h now' in script
    assert '--destination 169.254.169.254 --jump DROP' in script
    # 375 GiB data disk - 30 GiB reserved for images - 5 GiB per core for 16 cores
    assert 'UNRESERVED_WORKER_DATA_DISK_SIZE_GB=265' in script
    # python format braces must not leak into the shell script
    assert '{{' not in script.replace("'{{.Id}}'", '')


def test_user_data_unreserved_storage_for_job_private(global_config):
    script = gzip.decompress(vm_config(preemptible=False, job_private=True)['UserData']).decode()
    assert 'UNRESERVED_WORKER_DATA_DISK_SIZE_GB=375' in script


async def make_resource_manager(ec2_client, subnet_ids=('subnet-a', 'subnet-b', 'subnet-c')) -> AWSResourceManager:
    billing_manager = await AWSBillingManager.create(None)  # type: ignore
    return AWSResourceManager('us-east-1', ec2_client, billing_manager, launch_settings(subnet_ids))  # type: ignore


async def test_create_vm_retries_other_subnets_on_zonal_capacity_errors(global_config):
    ec2_client = FakeEc2Client()
    ec2_client.run_instances_errors = ['InsufficientInstanceCapacity', 'Unsupported']
    resource_manager = await make_resource_manager(ec2_client)

    config = instance_config()
    resources = await resource_manager.create_vm(
        FakeFileStore(),  # type: ignore
        'batch-worker-default-standard-abcde',
        'activation-token',
        60_000,
        False,
        375,
        30,
        True,
        False,
        'us-east-1',
        'm6i.4xlarge',
        config,
    )
    assert resources == []

    calls = ec2_client.run_instances_calls
    assert len(calls) == 3
    assert sorted(call['SubnetId'] for call in calls) == ['subnet-a', 'subnet-b', 'subnet-c']
    tokens = [call['ClientToken'] for call in calls]
    assert len(set(tokens)) == 3
    assert all(len(token) <= 64 for token in tokens)
    assert tokens[0] == client_token('batch-worker-default-standard-abcde', calls[0]['SubnetId'])


async def test_create_vm_stops_on_regional_errors(global_config):
    ec2_client = FakeEc2Client()
    ec2_client.run_instances_errors = ['VcpuLimitExceeded']
    resource_manager = await make_resource_manager(ec2_client)

    with pytest.raises(botocore.exceptions.ClientError):
        await resource_manager._run_instance_in_some_subnet('machine', {})
    assert len(ec2_client.run_instances_calls) == 1


async def test_create_vm_gives_up_after_every_subnet(global_config):
    ec2_client = FakeEc2Client()
    ec2_client.run_instances_errors = ['InsufficientInstanceCapacity'] * 2
    resource_manager = await make_resource_manager(ec2_client, subnet_ids=('subnet-a', 'subnet-b'))

    with pytest.raises(botocore.exceptions.ClientError):
        await resource_manager._run_instance_in_some_subnet('machine', {})
    assert len(ec2_client.run_instances_calls) == 2


async def test_get_vm_state_and_delete_vm():
    ec2_client = FakeEc2Client()
    resource_manager = await make_resource_manager(ec2_client)
    instance = FakeInstance('batch-worker-default-standard-abcde')

    with pytest.raises(VMDoesNotExist):
        await resource_manager.get_vm_state(instance)  # type: ignore
    assert ec2_client.filters['instances'] == [{'Name': 'tag:Name', 'Values': ['batch-worker-default-standard-abcde']}]
    with pytest.raises(VMDoesNotExist):
        await resource_manager.delete_vm(instance)  # type: ignore

    ec2_client.instances = [{'InstanceId': 'i-old', 'State': {'Name': 'terminated'}}]
    assert isinstance(await resource_manager.get_vm_state(instance), VMStateTerminated)  # type: ignore
    with pytest.raises(VMDoesNotExist):
        await resource_manager.delete_vm(instance)  # type: ignore

    ec2_client.instances = [
        {'InstanceId': 'i-old', 'State': {'Name': 'terminated'}},
        {'InstanceId': 'i-new', 'State': {'Name': 'pending'}},
    ]
    assert isinstance(await resource_manager.get_vm_state(instance), VMStateCreating)  # type: ignore
    await resource_manager.delete_vm(instance)  # type: ignore
    assert ec2_client.terminated == ['i-new']


async def test_resolve_worker_launch_settings():
    assert worker_image_name('default') == 'batch-worker-33'
    assert worker_image_name('dev-ns') == 'batch-worker-dev-ns-33'

    ec2_client = FakeEc2Client()
    with pytest.raises(ValueError, match='batch-worker-dev-ns-33'):
        await resolve_worker_launch_settings(ec2_client, 'dev-ns')  # type: ignore

    ec2_client.images = [{'ImageId': 'ami-123', 'RootDeviceName': '/dev/sda1'}]
    ec2_client.subnets = [
        {'SubnetId': 'subnet-a', 'VpcId': 'vpc-1'},
        {'SubnetId': 'subnet-b', 'VpcId': 'vpc-1'},
    ]
    ec2_client.security_groups = [{'GroupId': 'sg-123'}]
    settings = await resolve_worker_launch_settings(ec2_client, 'dev-ns')  # type: ignore

    assert settings.image_id == 'ami-123'
    assert settings.root_device_name == '/dev/sda1'
    assert settings.subnet_ids == ['subnet-a', 'subnet-b']
    assert settings.security_group_id == 'sg-123'
    assert settings.instance_profile_name == 'batch-worker'
    assert ec2_client.filters['images'] == (['self'], [{'Name': 'name', 'Values': ['batch-worker-dev-ns-33']}])
    assert ec2_client.filters['subnets'] == [{'Name': 'tag:hail-batch-worker', 'Values': ['true']}]
    assert ec2_client.filters['security_groups'] == [
        {'Name': 'group-name', 'Values': ['batch-worker']},
        {'Name': 'vpc-id', 'Values': ['vpc-1']},
    ]


def test_ecr_images_are_cloud_images():
    assert parse_docker_image_reference('123456789012.dkr.ecr.us-east-1.amazonaws.com/hail/ubuntu:24.04').hosted_in(
        'aws'
    )
    assert not parse_docker_image_reference('ubuntu:24.04').hosted_in('aws')
    assert not parse_docker_image_reference('us-docker.pkg.dev/hail-vdc/hail/ubuntu:24.04').hosted_in('aws')
