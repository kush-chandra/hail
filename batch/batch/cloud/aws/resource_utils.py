import logging
import math
from typing import Dict, List, Optional, Tuple

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

aws_valid_cores_from_worker_type: Dict[str, List[int]] = {
    worker_type: list(AWS_CORES_TO_INSTANCE_SIZE.keys()) for worker_type in AWS_WORKER_TYPE_TO_INSTANCE_FAMILY
}

aws_memory_to_worker_type = {'lowmem': 'highcpu', 'standard': 'standard', 'highmem': 'highmem'}


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
    for worker_type, valid_cores in aws_valid_cores_from_worker_type.items()
    for cores in valid_cores
]


def aws_machine_type_to_cores_and_memory_bytes(machine_type: str) -> Tuple[int, int]:
    parts = aws_machine_type_to_parts(machine_type)
    if parts is None:
        raise ValueError(f'bad machine_type: {machine_type}')
    return parts.cores, parts.memory


def aws_local_ssd_size(worker_type: str, cores: int) -> int:
    raise NotImplementedError('local SSD data disks are not supported on AWS')


def aws_requested_to_actual_storage_bytes(storage_bytes, allow_zero_storage):
    if storage_bytes > AWS_MAX_PERSISTENT_SSD_SIZE_GIB * 1024**3:
        return None
    if allow_zero_storage and storage_bytes == 0:
        return storage_bytes
    # keep the same 10 GiB minimum as gcp
    return max(10 * 1024**3, storage_bytes)


def aws_is_valid_storage_request(storage_in_gib: int) -> bool:
    return 10 <= storage_in_gib <= AWS_MAX_PERSISTENT_SSD_SIZE_GIB


def aws_cores_mcpu_to_memory_bytes(mcpu: int, worker_type: str) -> int:
    memory_bytes = AWS_MEMORY_PER_CORE_MIB[worker_type] * 1024**2
    return int((mcpu / 1000) * memory_bytes)


def aws_adjust_cores_for_memory_request(cores_in_mcpu: int, memory_in_bytes: int, worker_type: str) -> int:
    memory_per_core_bytes = AWS_MEMORY_PER_CORE_MIB[worker_type] * 1024**2
    min_cores_mcpu = math.ceil((memory_in_bytes / memory_per_core_bytes) * 1000)
    return max(cores_in_mcpu, min_cores_mcpu)
