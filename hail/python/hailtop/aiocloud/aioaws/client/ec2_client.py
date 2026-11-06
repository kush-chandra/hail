from typing import Any, Dict, List, Optional

import botocore.exceptions

from .base_client import AwsBaseClient


class AwsEc2Client(AwsBaseClient):
    def __init__(self, **kwargs):
        super().__init__('ec2', **kwargs)

    # https://docs.aws.amazon.com/AWSEC2/latest/APIReference/OperationList-query-ec2.html

    @staticmethod
    def error_code(e: botocore.exceptions.ClientError) -> Optional[str]:
        return e.response.get('Error', {}).get('Code')

    async def run_instances(self, **kwargs) -> Dict[str, Any]:
        resp = await self._call(self._client.run_instances, MinCount=1, MaxCount=1, **kwargs)
        return resp['Instances'][0]

    async def describe_instances(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        paginator = self._client.get_paginator('describe_instances')

        def list_all():
            return [
                instance
                for page in paginator.paginate(Filters=filters)
                for reservation in page['Reservations']
                for instance in reservation['Instances']
            ]

        return await self._call(list_all)

    async def terminate_instances(self, instance_ids: List[str]) -> None:
        await self._call(self._client.terminate_instances, InstanceIds=instance_ids)

    async def describe_images(self, owners: List[str], filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        resp = await self._call(self._client.describe_images, Owners=owners, Filters=filters)
        return resp['Images']

    async def describe_subnets(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        resp = await self._call(self._client.describe_subnets, Filters=filters)
        return resp['Subnets']

    async def describe_security_groups(self, filters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        resp = await self._call(self._client.describe_security_groups, Filters=filters)
        return resp['SecurityGroups']
