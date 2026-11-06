import logging
from typing import Optional, Tuple

log = logging.getLogger('resource_utils')

# gp3 volumes can be at most 16 TiB
AWS_MAX_PERSISTENT_SSD_SIZE_GIB = 16 * 1024

AWS_WORKER_TYPE_TO_INSTANCE_FAMILY = {
    'standard': 'm6i',
    'highmem': 'r6i',
    'highcpu': 'c6i',
}

AWS_INSTANCE_FAMILY_TO_WORKER_TYPE = {
    family: worker_type for worker_type, family in AWS_WORKER_TYPE_TO_INSTANCE_FAMILY.items()
}

AWS_MEMORY_PER_CORE_MIB = {
    'standard': 4096,
    'highmem': 8192,
    'highcpu': 2048,
}

# https://aws.amazon.com/ec2/instance-types/m6i/ (r6i and c6i share these sizes)
AWS_CORES_TO_INSTANCE_SIZE = {
    2: 'large',
    4: 'xlarge',
    8: '2xlarge',
    16: '4xlarge',
    32: '8xlarge',
    48: '12xlarge',
    64: '16xlarge',
    96: '24xlarge',
    128: '32xlarge',
}

AWS_INSTANCE_SIZE_TO_CORES = {size: cores for cores, size in AWS_CORES_TO_INSTANCE_SIZE.items()}


class MachineTypeParts:
    def __init__(self, family: str, worker_type: str, cores: int, memory: int):
        self.family = family
        self.worker_type = worker_type
        self.cores = cores
        self.memory = memory


def aws_worker_properties_to_machine_type(worker_type: str, cores: int) -> str:
    family = AWS_WORKER_TYPE_TO_INSTANCE_FAMILY[worker_type]
    size = AWS_CORES_TO_INSTANCE_SIZE[cores]
    return f'{family}.{size}'


def aws_machine_type_to_parts(machine_type: str) -> Optional[MachineTypeParts]:
    try:
        family, size = machine_type.split('.')
    except ValueError:
        return None
    worker_type = AWS_INSTANCE_FAMILY_TO_WORKER_TYPE.get(family)
    cores = AWS_INSTANCE_SIZE_TO_CORES.get(size)
    if worker_type is None or cores is None:
        return None
    memory = cores * AWS_MEMORY_PER_CORE_MIB[worker_type] * 1024**2
    return MachineTypeParts(family=family, worker_type=worker_type, cores=cores, memory=memory)


aws_valid_machine_types = [
    aws_worker_properties_to_machine_type(worker_type, cores)
    for worker_type in AWS_WORKER_TYPE_TO_INSTANCE_FAMILY
    for cores in AWS_CORES_TO_INSTANCE_SIZE
]


def aws_machine_type_to_cores_and_memory_bytes(machine_type: str) -> Tuple[int, int]:
    parts = aws_machine_type_to_parts(machine_type)
    if parts is None:
        raise ValueError(f'bad machine_type: {machine_type}')
    return parts.cores, parts.memory


def aws_requested_to_actual_storage_bytes(storage_bytes, allow_zero_storage):
    if storage_bytes > AWS_MAX_PERSISTENT_SSD_SIZE_GIB * 1024**3:
        return None
    if allow_zero_storage and storage_bytes == 0:
        return storage_bytes
    # keep the same 10 GiB minimum as gcp
    return max(10 * 1024**3, storage_bytes)


def aws_is_valid_storage_request(storage_in_gib: int) -> bool:
    return 10 <= storage_in_gib <= AWS_MAX_PERSISTENT_SSD_SIZE_GIB
