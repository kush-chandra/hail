import hashlib
import logging
import random
from typing import Any, Dict, List, Optional

import botocore.exceptions

from hailtop.aiocloud import aioaws

from ....driver.instance import Instance
from ....driver.resource_manager import (
    CloudResourceManager,
    UnknownVMState,
    VMDoesNotExist,
    VMState,
    VMStateCreating,
    VMStateRunning,
    VMStateTerminated,
)
from ....file_store import FileStore
from ....instance_config import InstanceConfig, QuantifiedResource
from ..instance_config import AWSSlimInstanceConfig
from ..resource_utils import aws_machine_type_to_cores_and_memory_bytes, aws_worker_properties_to_machine_type
from .billing_manager import AWSBillingManager
from .create_instance import create_vm_config

log = logging.getLogger('resource_manager')

# Errors that only apply to the availability zone of the subnet we tried, so another subnet may succeed
# https://docs.aws.amazon.com/AWSEC2/latest/APIReference/errors-overview.html
ZONAL_CAPACITY_ERROR_CODES = {'InsufficientInstanceCapacity', 'Unsupported'}


class AWSWorkerLaunchSettings:
    def __init__(
        self,
        image_id: str,
        root_device_name: str,
        subnet_ids: List[str],
        security_group_id: str,
        instance_profile_name: str,
    ):
        assert subnet_ids
        self.image_id = image_id
        self.root_device_name = root_device_name
        self.subnet_ids = subnet_ids
        self.security_group_id = security_group_id
        self.instance_profile_name = instance_profile_name


def client_token(machine_name: str, subnet_id: str) -> str:
    # RunInstances is idempotent per client token, but a token cannot be reused with different
    # parameters, so each subnet attempt gets its own token. Tokens are at most 64 characters.
    return hashlib.sha256(f'{machine_name}/{subnet_id}'.encode()).hexdigest()


class AWSResourceManager(CloudResourceManager):
    def __init__(
        self,
        region: str,
        ec2_client: aioaws.AwsEc2Client,  # BORROWED
        billing_manager: AWSBillingManager,  # BORROWED
        launch_settings: AWSWorkerLaunchSettings,
    ):
        self.region = region
        self.ec2_client = ec2_client
        self.billing_manager = billing_manager
        self.launch_settings = launch_settings

    async def _describe_instance(self, instance: Instance) -> Optional[Dict[str, Any]]:
        ec2_instances = await self.ec2_client.describe_instances([{'Name': 'tag:Name', 'Values': [instance.name]}])
        if not ec2_instances:
            return None
        live = [i for i in ec2_instances if i['State']['Name'] != 'terminated']
        if live:
            return live[0]
        return ec2_instances[0]

    async def delete_vm(self, instance: Instance):
        ec2_instance = await self._describe_instance(instance)
        if ec2_instance is None or ec2_instance['State']['Name'] == 'terminated':
            raise VMDoesNotExist()
        await self.ec2_client.terminate_instances([ec2_instance['InstanceId']])

    async def get_vm_state(self, instance: Instance) -> VMState:
        ec2_instance = await self._describe_instance(instance)
        if ec2_instance is None:
            # terminated instances disappear from DescribeInstances after about an hour
            raise VMDoesNotExist()
        return ec2_instance_to_vm_state(ec2_instance, instance.time_created)

    def machine_type(self, cores: int, worker_type: str, local_ssd: bool) -> str:  # pylint: disable=unused-argument
        return aws_worker_properties_to_machine_type(worker_type, cores)

    def instance_config(
        self,
        machine_type: str,
        preemptible: bool,
        local_ssd_data_disk: bool,
        data_disk_size_gb: int,
        boot_disk_size_gb: int,
        job_private: bool,
        location: str,
    ) -> AWSSlimInstanceConfig:
        return AWSSlimInstanceConfig.create(
            self.billing_manager.product_versions,
            machine_type,
            preemptible,
            local_ssd_data_disk,
            data_disk_size_gb,
            boot_disk_size_gb,
            job_private,
            location,
        )

    async def create_vm(
        self,
        file_store: FileStore,
        machine_name: str,
        activation_token: str,
        max_idle_time_msecs: int,
        local_ssd_data_disk: bool,
        data_disk_size_gb: int,
        boot_disk_size_gb: int,
        preemptible: bool,
        job_private: bool,
        location: str,  # pylint: disable=unused-argument
        machine_type: str,
        instance_config: InstanceConfig,
    ) -> List[QuantifiedResource]:
        assert not local_ssd_data_disk, 'local SSD data disks are not supported on AWS'

        vm_config = create_vm_config(
            file_store,
            self.billing_manager.resource_rates,
            self.region,
            machine_name,
            machine_type,
            activation_token,
            max_idle_time_msecs,
            data_disk_size_gb,
            boot_disk_size_gb,
            preemptible,
            job_private,
            self.launch_settings.image_id,
            self.launch_settings.root_device_name,
            self.launch_settings.security_group_id,
            self.launch_settings.instance_profile_name,
            instance_config,
        )

        cores, memory_in_bytes = aws_machine_type_to_cores_and_memory_bytes(machine_type)
        total_resources_on_instance = instance_config.quantified_resources(
            cpu_in_mcpu=cores * 1000, memory_in_bytes=memory_in_bytes, extra_storage_in_gib=0
        )

        try:
            await self._run_instance_in_some_subnet(machine_name, vm_config)
        except Exception:
            log.exception(f'error while creating machine {machine_name}')
        return total_resources_on_instance

    async def _run_instance_in_some_subnet(self, machine_name: str, vm_config: Dict[str, Any]) -> None:
        subnet_ids = list(self.launch_settings.subnet_ids)
        random.shuffle(subnet_ids)
        for i, subnet_id in enumerate(subnet_ids):
            try:
                ec2_instance = await self.ec2_client.run_instances(
                    SubnetId=subnet_id,
                    ClientToken=client_token(machine_name, subnet_id),
                    **vm_config,
                )
                log.info(f'created machine {machine_name} as {ec2_instance["InstanceId"]} in subnet {subnet_id}')
                return
            except botocore.exceptions.ClientError as e:
                code = aioaws.AwsEc2Client.error_code(e)
                if code in ZONAL_CAPACITY_ERROR_CODES and i + 1 < len(subnet_ids):
                    log.warning(f'{code} creating machine {machine_name} in subnet {subnet_id}, trying another subnet')
                    continue
                raise


def ec2_instance_to_vm_state(ec2_instance: Dict[str, Any], time_created: int) -> VMState:
    # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-lifecycle.html
    state = ec2_instance['State']['Name']
    if state == 'pending':
        return VMStateCreating(ec2_instance, time_created)
    if state == 'running':
        launch_time_msecs = int(ec2_instance['LaunchTime'].timestamp() * 1000)
        return VMStateRunning(ec2_instance, launch_time_msecs)
    if state in ('shutting-down', 'terminated', 'stopping', 'stopped'):
        return VMStateTerminated(ec2_instance)
    log.error(f'Unknown ec2 state {state} for {ec2_instance.get("InstanceId")}')
    return UnknownVMState(ec2_instance)
