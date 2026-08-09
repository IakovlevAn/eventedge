from __future__ import annotations

import asyncio
import os

import ydb

from eventedge.storage import migrate_ydb_schema


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


async def main() -> None:
    await migrate_ydb_schema(
        endpoint=required_environment("YDB_ENDPOINT"),
        database=required_environment("YDB_DATABASE"),
        credentials=ydb.AccessTokenCredentials(
            required_environment("YDB_ACCESS_TOKEN")
        ),
    )
    print("YDB schema migration completed")


if __name__ == "__main__":
    asyncio.run(main())
