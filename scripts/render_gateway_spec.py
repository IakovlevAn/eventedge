from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the EventEdge API Gateway specification")
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--service-account-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    template = Path("infra/api-gateway.template.yaml").read_text(encoding="utf-8")
    rendered = template.replace("__API_CONTAINER_ID__", args.container_id).replace(
        "__GATEWAY_SERVICE_ACCOUNT_ID__", args.service_account_id
    )
    if "__" in rendered:
        raise ValueError("The rendered API Gateway specification still contains placeholders")
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
