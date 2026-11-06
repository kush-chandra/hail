import base64
from typing import Tuple

from .base_client import AwsBaseClient


class AwsEcrClient(AwsBaseClient):
    def __init__(self, **kwargs):
        super().__init__('ecr', **kwargs)

    # https://docs.aws.amazon.com/AmazonECR/latest/APIReference/API_GetAuthorizationToken.html
    async def get_docker_credentials(self) -> Tuple[str, str]:
        resp = await self._call(self._client.get_authorization_token)
        token = resp['authorizationData'][0]['authorizationToken']
        username, password = base64.b64decode(token).decode().split(':', maxsplit=1)
        return username, password
