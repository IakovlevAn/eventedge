from __future__ import annotations

import os
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

from scripts.deploy_serverless import request_json, wait_for_operation
from scripts.render_gateway_spec import render_gateway_spec

UPDATE_URL = (
    "https://serverless-apigateway.api.cloud.yandex.net/"
    "apigateways/v1/apigateways/{}"
)


def build_payload(openapi_spec: str) -> dict[str, str]:
    return {
        "updateMask": "openapiSpec",
        "openapiSpec": openapi_spec,
    }


def rendered_spec(environment: Mapping[str, str]) -> str:
    template = Path("infra/api-gateway.template.yaml").read_text(encoding="utf-8")
    return render_gateway_spec(
        template,
        container_id=environment["YC_CONTAINER_ID"],
        service_account_id=environment["YC_GATEWAY_SERVICE_ACCOUNT_ID"],
    )


def main() -> None:
    token = os.environ["IAM_TOKEN"]
    gateway_id = urllib.parse.quote(os.environ["YC_GATEWAY_ID"], safe="")
    operation = request_json(
        UPDATE_URL.format(gateway_id),
        token=token,
        method="PATCH",
        payload=build_payload(rendered_spec(os.environ)),
    )
    wait_for_operation(operation, token=token)
    print(f"API Gateway {gateway_id} specification deployed")


if __name__ == "__main__":
    main()
