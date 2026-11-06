import logging

from ....worker.disk import CloudDisk

log = logging.getLogger('disk')


# TODO: create and attach an EBS volume to the running worker
class AWSDisk(CloudDisk):
    def __init__(self, name: str, instance_name: str, size_in_gb: int, mount_path: str):
        assert size_in_gb >= 10

        self.name = name
        self.instance_name = instance_name
        self.size_in_gb = size_in_gb
        self.mount_path = mount_path

    async def create(self, labels=None):
        raise NotImplementedError

    async def delete(self):
        raise NotImplementedError
