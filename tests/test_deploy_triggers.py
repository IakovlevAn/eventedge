from scripts.deploy_triggers import TIMER_SPECS, create_payload, update_payload

ENVIRONMENT = {
    "YC_FOLDER_ID": "folder-id",
    "YC_CONTAINER_ID": "container-id",
    "YC_WORKER_CONTAINER_ID": "worker-container-id",
    "YC_GATEWAY_SERVICE_ACCOUNT_ID": "gateway-sa-id",
}


def test_fast_news_timer_runs_every_minute() -> None:
    fast = TIMER_SPECS[0]
    payload = create_payload(fast, ENVIRONMENT)

    assert payload["name"] == "eventedge-fast-news"
    assert payload["folderId"] == "folder-id"
    assert payload["rule"] == {
        "timer": {
            "cronExpression": "* * ? * * *",
            "payload": "fast_news",
            "invokeContainerWithRetry": {
                "containerId": "worker-container-id",
                "serviceAccountId": "gateway-sa-id",
                "retrySettings": {
                    "retryAttempts": "3",
                    "interval": "30s",
                },
            },
        }
    }


def test_collection_lanes_have_distinct_schedules_and_payloads() -> None:
    assert [(spec.cron_expression, spec.payload) for spec in TIMER_SPECS] == [
        ("* * ? * * *", "fast_news"),
        ("0/5 * ? * * *", "discovery_news"),
        ("0/15 * ? * * *", "slow_news"),
    ]


def test_update_replaces_complete_managed_trigger_configuration() -> None:
    payload = update_payload(TIMER_SPECS[0], ENVIRONMENT)

    assert "folderId" not in payload
    assert payload["updateMask"] == "name,description,labels,rule"
