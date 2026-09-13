from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from eventedge_research.ydb_signal_export import (
    YDB_EXPORT_TABLES_BY_NAME,
    YDB_SIGNAL_TABLE_NAMES,
    export_ydb_signal_data,
    parse_ydb_connection_string,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export fixed EventEdge signal tables through independent SELECT-only "
            "pages without claiming a shared point-in-time snapshot"
        )
    )
    parser.add_argument(
        "--connection-string",
        required=True,
        help="YDB grpcs:// connection string containing the database query parameter",
    )
    parser.add_argument("--service-account-key-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--tables",
        nargs="+",
        choices=tuple(YDB_EXPORT_TABLES_BY_NAME),
        default=YDB_SIGNAL_TABLE_NAMES,
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--max-rows-per-table", type=int, default=100_000)
    parser.add_argument("--query-timeout-seconds", type=float, default=60)
    args = parser.parse_args()

    manifest = asyncio.run(
        export_ydb_signal_data(
            connection=parse_ydb_connection_string(args.connection_string),
            service_account_key_file=args.service_account_key_file,
            output_directory=args.output_dir,
            table_names=args.tables,
            batch_size=args.batch_size,
            max_rows_per_table=args.max_rows_per_table,
            query_timeout_seconds=args.query_timeout_seconds,
        )
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
