from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass

from scripts.deploy_serverless import masked

TRIGGERS_URL = "https://serverless-triggers.api.cloud.yandex.net/triggers/v1/triggers"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{}"
LEGACY_FAST_TRIGGER_NAME = "eventedge-moex-news"


@dataclass(frozen=True)
class TimerSpec:
    name: str
    description: str
    cron_expression: str
    payload: str


TIMER_SPECS = (
    TimerSpec(
        name="eventedge-fast-news",
        description="Priority market news every minute",
        cron_expression="* * ? * * *",
        payload="fast_news",
    ),
    TimerSpec(
        name="eventedge-discovery-news",
        description="Broad market news discovery every five minutes",
        cron_expression="0/5 * ? * * *",
        payload="discovery_news",
    ),
    TimerSpec(
        name="eventedge-slow-news",
        description="Macro context refresh every fifteen minutes",
        cron_expression="0/15 * ? * * *",
        payload="slow_news",
    ),
)


def request_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        response_text = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Yandex Cloud Triggers API HTTP {exc.code}: {masked(response_text)}"
        ) from exc


def timer_rule(spec: TimerSpec, environment: Mapping[str, str]) -> dict[str, object]:
    return {
        "timer": {
            "cronExpression": spec.cron_expression,
            "payload": spec.payload,
            "invokeContainerWithRetry": {
                "containerId": environment["YC_CONTAINER_ID"],
                "serviceAccountId": environment["YC_GATEWAY_SERVICE_ACCOUNT_ID"],
                "retrySettings": {
                    "retryAttempts": "3",
                    "interval": "30s",
                },
            },
        }
    }


def create_payload(spec: TimerSpec, environment: Mapping[str, str]) -> dict[str, object]:
    return {
        "folderId": environment["YC_FOLDER_ID"],
        "name": spec.name,
        "description": spec.description,
        "labels": {"managed-by": "eventedge-ci", "collection-lane": spec.payload},
        "rule": timer_rule(spec, environment),
    }


def update_payload(spec: TimerSpec, environment: Mapping[str, str]) -> dict[str, object]:
    payload = create_payload(spec, environment)
    payload.pop("folderId")
    payload["updateMask"] = "name,description,labels,rule"
    return payload


def list_triggers(*, token: str, folder_id: str) -> list[dict[str, object]]:
    query = urllib.parse.urlencode({"folderId": folder_id, "pageSize": 100})
    response = request_json(f"{TRIGGERS_URL}?{query}", token=token)
    return list(response.get("triggers", []))


def wait_for_operation(operation: dict[str, object], *, token: str) -> None:
    operation_id = str(operation["id"])
    current = operation
    for _ in range(60):
        if current.get("error"):
            raise RuntimeError(
                "Yandex Cloud trigger operation failed: "
                + masked(json.dumps(current["error"], ensure_ascii=False))
            )
        if current.get("done"):
            return
        time.sleep(2)
        encoded_id = urllib.parse.quote(operation_id, safe="")
        current = request_json(OPERATION_URL.format(encoded_id), token=token)
    raise TimeoutError(
        f"Yandex Cloud trigger operation {operation_id} did not finish in 120 seconds"
    )


def deploy_triggers(environment: Mapping[str, str]) -> None:
    token = environment["IAM_TOKEN"]
    existing = list_triggers(token=token, folder_id=environment["YC_FOLDER_ID"])
    by_name = {str(trigger["name"]): trigger for trigger in existing}

    for spec in TIMER_SPECS:
        trigger = by_name.get(spec.name)
        if spec.payload == "fast_news" and trigger is None:
            trigger = by_name.get(LEGACY_FAST_TRIGGER_NAME)

        if trigger is None:
            operation = request_json(
                TRIGGERS_URL,
                token=token,
                method="POST",
                payload=create_payload(spec, environment),
            )
            action = "created"
        else:
            trigger_id = urllib.parse.quote(str(trigger["id"]), safe="")
            operation = request_json(
                f"{TRIGGERS_URL}/{trigger_id}",
                token=token,
                method="PATCH",
                payload=update_payload(spec, environment),
            )
            action = "updated"

        wait_for_operation(operation, token=token)
        print(f"Trigger {spec.name} {action}: {spec.cron_expression} -> {spec.payload}")


def main() -> None:
    deploy_triggers(os.environ)


if __name__ == "__main__":
    main()
