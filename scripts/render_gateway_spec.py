from __future__ import annotations

import argparse
from pathlib import Path


def render_gateway_spec(
    template: str,
    *,
    container_id: str,
    service_account_id: str,
) -> str:
    rendered = template.replace("__API_CONTAINER_ID__", container_id).replace(
        "__GATEWAY_SERVICE_ACCOUNT_ID__", service_account_id
    )
    placeholders = ("__API_CONTAINER_ID__", "__GATEWAY_SERVICE_ACCOUNT_ID__")
    if any(placeholder in rendered for placeholder in placeholders):
        raise ValueError("The rendered API Gateway specification still contains placeholders")
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the EventEdge API Gateway specification")
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--service-account-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    template = Path("infra/api-gateway.template.yaml").read_text(encoding="utf-8")
    rendered = render_gateway_spec(
        template,
        container_id=args.container_id,
        service_account_id=args.service_account_id,
    )
    args.output.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
