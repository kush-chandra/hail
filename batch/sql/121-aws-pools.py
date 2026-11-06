import asyncio
import os

from gear import Database


async def main():
    if os.environ['HAIL_CLOUD'] != 'aws':
        return

    db = Database()
    await db.async_init()
    try:
        # inst_colls.cloud defaults to gcp. The pools keep their gcp worker types (standard, highmem,
        # highcpu), which the AWS driver maps to the m6i, r6i, and c6i instance families.
        await db.just_execute(
            """
UPDATE inst_colls SET cloud = 'aws';
"""
        )

        # AWS workers do not support local SSDs yet, so give every pool a gp3 data disk instead. 375 GiB
        # matches one GCP local SSD partition, and the disk must hold at least the reserved image and
        # per-core container storage (see unreserved_worker_data_disk_size_gib).
        await db.just_execute(
            """
UPDATE pools
SET
  worker_local_ssd_data_disk = FALSE,
  worker_external_ssd_data_disk_size_gb = GREATEST(375, 30 + 5 * GREATEST(worker_cores, standing_worker_cores));
"""
        )
    finally:
        await db.async_close()


asyncio.run(main())
