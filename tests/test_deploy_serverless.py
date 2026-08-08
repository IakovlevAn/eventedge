from scripts.deploy_serverless import build_payload, masked


def test_deployment_payload_has_budget_caps() -> None:
    payload = build_payload(
        {
            "YC_CONTAINER_ID": "container-id",
            "YC_RUNTIME_SERVICE_ACCOUNT_ID": "runtime-sa-id",
            "IMAGE_URL": "cr.yandex/registry/eventedge-api:sha",
            "DEPLOY_SHA": "abc123",
            "YDB_ENDPOINT": "grpcs://ydb.example:2135",
            "YDB_DATABASE": "/region/cloud/database",
        }
    )

    assert payload["resources"] == {
        "memory": "268435456",
        "cores": "1",
        "coreFraction": "100",
    }
    assert payload["provisionPolicy"] == {"minInstances": "0"}
    assert payload["executionTimeout"] == "30s"
    assert payload["scalingPolicy"] == {
        "zoneInstancesLimit": "1",
        "zoneRequestsLimit": "50",
    }
    assert payload["runtime"] == {"http": {}}
    assert payload["imageSpec"]["environment"] == {
        "APP_ENV": "prod",
        "YDB_ENDPOINT": "grpcs://ydb.example:2135",
        "YDB_DATABASE": "/region/cloud/database",
    }


def test_api_error_masker_hides_tokens() -> None:
    assert masked("failed t1_secret-value") == "failed ***"
