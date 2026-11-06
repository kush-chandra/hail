from typing import List

from ...driver.billing_manager import ProductVersions
from ...instance_config import InstanceConfig
from ...resources import Resource
from .resource_utils import aws_machine_type_to_parts

AWS_INSTANCE_CONFIG_VERSION = 1


class AWSSlimInstanceConfig(InstanceConfig):
    @staticmethod
    def create(
        product_versions: ProductVersions,  # pylint: disable=unused-argument
        machine_type: str,
        preemptible: bool,
        local_ssd_data_disk: bool,
        data_disk_size_gb: int,
        boot_disk_size_gb: int,
        job_private: bool,
        location: str,  # pylint: disable=unused-argument
    ) -> 'AWSSlimInstanceConfig':
        # TODO: billing is not implemented on AWS yet, so instances have no billable resources and cost nothing
        return AWSSlimInstanceConfig(
            machine_type=machine_type,
            preemptible=preemptible,
            local_ssd_data_disk=local_ssd_data_disk,
            data_disk_size_gb=data_disk_size_gb,
            boot_disk_size_gb=boot_disk_size_gb,
            job_private=job_private,
            resources=[],
        )

    def __init__(
        self,
        machine_type: str,
        preemptible: bool,
        local_ssd_data_disk: bool,
        data_disk_size_gb: int,
        boot_disk_size_gb: int,
        job_private: bool,
        resources: List[Resource],
    ):
        self.cloud = 'aws'
        self._machine_type = machine_type
        self.preemptible = preemptible
        self.local_ssd_data_disk = local_ssd_data_disk
        self.data_disk_size_gb = data_disk_size_gb
        self.job_private = job_private
        self.boot_disk_size_gb = boot_disk_size_gb
        self.resources = resources

        machine_type_parts = aws_machine_type_to_parts(self._machine_type)
        assert machine_type_parts is not None, machine_type
        self.machine_type_parts = machine_type_parts
        self._worker_type = machine_type_parts.worker_type
        self.cores = machine_type_parts.cores

    def worker_type(self) -> str:
        return self._worker_type

    def instance_memory(self) -> int:
        return self.machine_type_parts.memory

    def region_for(self, location: str) -> str:
        # location = region
        return location

    @staticmethod
    def from_dict(data: dict) -> 'AWSSlimInstanceConfig':
        assert data['version'] == AWS_INSTANCE_CONFIG_VERSION, data
        assert data['resources'] == [], data
        return AWSSlimInstanceConfig(
            data['machine_type'],
            data['preemptible'],
            data['local_ssd_data_disk'],
            data['data_disk_size_gb'],
            data['boot_disk_size_gb'],
            data['job_private'],
            [],
        )

    def to_dict(self) -> dict:
        return {
            'version': AWS_INSTANCE_CONFIG_VERSION,
            'cloud': 'aws',
            'machine_type': self._machine_type,
            'preemptible': self.preemptible,
            'local_ssd_data_disk': self.local_ssd_data_disk,
            'data_disk_size_gb': self.data_disk_size_gb,
            'boot_disk_size_gb': self.boot_disk_size_gb,
            'job_private': self.job_private,
            'resources': [resource.to_dict() for resource in self.resources],
        }
