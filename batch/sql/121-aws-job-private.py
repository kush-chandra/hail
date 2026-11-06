import asyncio
import os

from gear import Database


async def main():
    if os.environ['HAIL_CLOUD'] != 'aws':
        return

    db = Database()
    await db.async_init()
    try:
        # inst_colls.cloud defaults to gcp. Only job private instances are supported on AWS for now, so the
        # pools are left alone and the AWS driver does not start them.
        await db.just_execute(
            """
UPDATE inst_colls SET cloud = 'aws' WHERE name = 'job-private';
"""
        )
    finally:
        await db.async_close()


asyncio.run(main())
