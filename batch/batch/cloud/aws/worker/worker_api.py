import base64
import os
from typing import Dict, List

import boto3
import orjson
from aiohttp import web

from hailtop import httpx
from hailtop.aiocloud import aioaws
from hailtop.auth.auth import IdentityProvider

from ....worker.worker_api import CloudWorkerAPI, ContainerRegistryCredentials
from ..instance_config import AWSSlimInstanceConfig
from .disk import AWSDisk


class AWSWorkerAPI(CloudWorkerAPI):
    # the Amazon-provided DNS server of the VPC
    # https://docs.aws.amazon.com/vpc/latest/userguide/AmazonDNS-concepts.html
    nameserver_ip = '169.254.169.253'

    @staticmethod
    def from_env() -> 'AWSWorkerAPI':
        region = os.environ['REGION']
        return AWSWorkerAPI(region)

    def __init__(self, region: str):
        self.region = region
        # the worker's own credentials come from the instance profile
        self._ecr_client = aioaws.AwsEcrClient(session=boto3.session.Session(region_name=region))

    @property
    def cloud_specific_env_vars_for_user_jobs(self) -> List[str]:
        idp_json = orjson.dumps({'idp': IdentityProvider.AMAZON.value}).decode('utf-8')
        return [
            'AWS_APPLICATION_CREDENTIALS=/gsa-key/key.json',
            f'AWS_REGION={self.region}',
            f'HAIL_IDENTITY_PROVIDER_JSON={idp_json}',
        ]

    def create_disk(self, instance_name: str, disk_name: str, size_in_gb: int, mount_path: str) -> AWSDisk:
        return AWSDisk(disk_name, instance_name, size_in_gb, mount_path)

    async def worker_container_registry_credentials(self, session: httpx.ClientSession) -> ContainerRegistryCredentials:
        username, password = await self._ecr_client.get_docker_credentials()
        return {'username': username, 'password': password}

    async def user_container_registry_credentials(self, credentials: Dict[str, str]) -> ContainerRegistryCredentials:
        key = orjson.loads(base64.b64decode(credentials['key.json']).decode())
        session = boto3.session.Session(
            aws_access_key_id=key['AccessKeyId'],
            aws_secret_access_key=key['SecretAccessKey'],
            region_name=self.region,
        )
        async with aioaws.AwsEcrClient(session=session) as ecr_client:
            username, password = await ecr_client.get_docker_credentials()
        return {'username': username, 'password': password}

    def create_metadata_server_app(self, credentials: Dict[str, str]) -> web.Application:
        raise NotImplementedError

    def instance_config_from_config_dict(self, config_dict: Dict[str, str]) -> AWSSlimInstanceConfig:
        return AWSSlimInstanceConfig.from_dict(config_dict)

    # TODO: mount S3 buckets with mountpoint-s3
    async def _mount_cloudfuse(
        self,
        credentials: Dict[str, str],
        mount_base_path_data: str,
        mount_base_path_tmp: str,
        config: dict,
    ):
        raise NotImplementedError

    async def unmount_cloudfuse(self, mount_base_path_data: str):
        raise NotImplementedError

    async def close(self):
        await self._ecr_client.close()

    def __str__(self):
        return f'region={self.region}'
