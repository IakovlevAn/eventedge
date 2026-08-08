from scripts.deploy_gateway import build_payload, rendered_spec


def test_gateway_deployment_updates_only_openapi_spec() -> None:
    payload = build_payload("openapi: 3.0.0\n")

    assert payload == {
        "updateMask": "openapiSpec",
        "openapiSpec": "openapi: 3.0.0\n",
    }


def test_gateway_deployment_renders_current_routes() -> None:
    spec = rendered_spec(
        {
            "YC_CONTAINER_ID": "container-id",
            "YC_GATEWAY_SERVICE_ACCOUNT_ID": "gateway-sa-id",
        }
    )

    assert "/v1/instruments/snapshots:" in spec
    assert "container_id: container-id" in spec
    assert "service_account_id: gateway-sa-id" in spec
    assert "__API_CONTAINER_ID__" not in spec
