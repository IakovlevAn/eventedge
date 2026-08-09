from pathlib import Path

import yaml

from scripts.deploy_serverless import build_payload, masked


def test_deployment_payload_has_budget_caps() -> None:
    payload = build_payload(
        {
            "YC_CONTAINER_ID": "container-id",
            "YC_RUNTIME_SERVICE_ACCOUNT_ID": "runtime-sa-id",
            "YC_FOLDER_ID": "folder-id",
            "IMAGE_URL": "cr.yandex/registry/eventedge-api:sha",
            "DEPLOY_SHA": "abc123",
            "YDB_ENDPOINT": "grpcs://ydb.example:2135",
            "YDB_DATABASE": "/region/cloud/database",
        }
    )

    assert payload["resources"] == {
        "memory": "536870912",
        "cores": "1",
        "coreFraction": "100",
    }
    assert payload["concurrency"] == "2"
    assert payload["provisionPolicy"] == {"minInstances": "0"}
    assert payload["executionTimeout"] == "60s"
    assert payload["scalingPolicy"] == {
        "zoneInstancesLimit": "1",
        "zoneRequestsLimit": "50",
    }
    assert payload["runtime"] == {"http": {}}
    assert payload["imageSpec"]["environment"] == {
        "APP_ENV": "prod",
        "APP_REVISION": "abc123",
        "YDB_ENDPOINT": "grpcs://ydb.example:2135",
        "YDB_DATABASE": "/region/cloud/database",
        "YANDEX_GPT_ENABLED": "true",
        "YANDEX_GPT_FOLDER_ID": "folder-id",
        "YANDEX_GPT_MODEL": "yandexgpt-lite",
    }


def test_api_error_masker_hides_tokens() -> None:
    assert masked("failed t1_secret-value") == "failed ***"


def test_admin_key_is_forwarded_only_when_configured() -> None:
    environment = {
        "YC_CONTAINER_ID": "container-id",
        "YC_RUNTIME_SERVICE_ACCOUNT_ID": "runtime-sa-id",
        "YC_FOLDER_ID": "folder-id",
        "IMAGE_URL": "cr.yandex/registry/eventedge-api:sha",
        "DEPLOY_SHA": "abc123",
        "YDB_ENDPOINT": "grpcs://ydb.example:2135",
        "YDB_DATABASE": "/region/cloud/database",
        "EVENTEDGE_ADMIN_KEY": "example-test-key",
    }

    payload = build_payload(environment)

    assert payload["imageSpec"]["environment"]["EVENTEDGE_ADMIN_KEY"] == "example-test-key"


def test_budget_policy_matches_deployment_caps() -> None:
    policy = yaml.safe_load(Path("infra/budget-policy.yaml").read_text(encoding="utf-8"))
    runtime = policy["runtime_caps"]["serverless_container"]

    assert policy["currency"] == "RUB"
    assert policy["monthly_budget"] == 10_000
    assert policy["notifications"]["thresholds_percent"] == [50, 80, 95]
    assert policy["enforcement"] == {
        "billing_budget": "notification_only",
        "automatic_shutdown": False,
    }
    assert runtime == {
        "memory_mb": 512,
        "cores": 1,
        "concurrency": 2,
        "min_instances": 0,
        "zone_instances_limit": 1,
        "zone_requests_limit": 50,
        "execution_timeout_seconds": 60,
    }
