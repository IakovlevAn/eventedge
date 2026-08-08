from pathlib import Path

import yaml

from scripts.render_gateway_spec import render_gateway_spec


def test_gateway_template_exposes_web_app_and_assets() -> None:
    template = Path("infra/api-gateway.template.yaml").read_text(encoding="utf-8")
    rendered = render_gateway_spec(
        template,
        container_id="container-id",
        service_account_id="gateway-sa-id",
    )
    spec = yaml.safe_load(rendered)

    assert "/" in spec["paths"]
    assert "/assets/{asset}" in spec["paths"]
    assert rendered.count("container_id: container-id") == len(spec["paths"])
    assert rendered.count("service_account_id: gateway-sa-id") == len(spec["paths"])
    assert "__API_CONTAINER_ID__" not in rendered
    assert "__GATEWAY_SERVICE_ACCOUNT_ID__" not in rendered
