from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping

DEPLOY_URL = "https://serverless-containers.api.cloud.yandex.net/containers/v1/revisions:deploy"
OPERATION_URL = "https://operation.api.cloud.yandex.net/operations/{}"
SECRET_PATTERN = re.compile(r"(?:y[01]_|t[01]_|AQAD-)[A-Za-z0-9_-]+")


def build_payload(
    environment: Mapping[str, str],
    *,
    component: str = "api",
) -> dict[str, object]:
    if component not in {"api", "worker"}:
        raise ValueError(f"Unsupported EventEdge component: {component}")
    is_worker = component == "worker"
    runtime_environment = {
        "APP_ENV": "prod",
        "APP_REVISION": environment["DEPLOY_SHA"],
        "EVENTEDGE_COMPONENT": component,
        "YDB_ENDPOINT": environment["YDB_ENDPOINT"],
        "YDB_DATABASE": environment["YDB_DATABASE"],
        "YDB_POOL_SIZE": "8",
        "YANDEX_GPT_ENABLED": "true",
        "YANDEX_GPT_FOLDER_ID": environment["YC_FOLDER_ID"],
        "YANDEX_GPT_MODEL": "yandexgpt-lite",
        # Keep one LLM request in flight: concurrent calls share the runtime
        # client and were cancelled by the serverless request lifecycle.
        # Raw news collection is independent, so deferred candidates are safe
        # to finish in later idempotent waves. Maintenance deadlines remain
        # bounded below the revision's 180-second execution timeout.
        "BACKFILL_BATCH_LIMIT": "1",
        "BACKFILL_CONCURRENCY": "1",
        "EVENTEDGE_MONTHLY_BUDGET_RUB": "12000",
    }
    if admin_key := environment.get("EVENTEDGE_ADMIN_KEY"):
        runtime_environment["EVENTEDGE_ADMIN_KEY"] = admin_key
    return {
        "containerId": (
            environment["YC_WORKER_CONTAINER_ID"] if is_worker else environment["YC_CONTAINER_ID"]
        ),
        "description": f"GitHub {environment['DEPLOY_SHA']} ({component})",
        "resources": {
            # Yandex Cloud requires at least 4 GiB to allocate 2 full vCPUs.
            # Both contours use the 2-vCPU tier. This prevents cold read-model
            # projection and timer collection from saturating a single core.
            "memory": "4294967296",
            "cores": "2",
            "coreFraction": "100",
        },
        "executionTimeout": "180s",
        "serviceAccountId": environment["YC_RUNTIME_SERVICE_ACCOUNT_ID"],
        "imageSpec": {
            "imageUrl": environment["IMAGE_URL"],
            "environment": runtime_environment,
        },
        # The public API deliberately keeps one warm instance as the single
        # writer for short-lived market snapshots. Its work is I/O-bound, so
        # seven concurrent requests cover one dashboard refresh plus burst
        # reads without creating a second process-local MOEX cache.
        "concurrency": "1" if is_worker else "7",
        "provisionPolicy": {"minInstances": "0" if is_worker else "1"},
        "scalingPolicy": {
            # Peak allocation is quota-safe: API 1x(2 CPU, 4 GiB) plus worker
            # 3x(2 CPU, 4 GiB) = 8 CPU and 16 GiB. Keeping the public API at one
            # instance prevents load balancing between divergent local caches.
            "zoneInstancesLimit": "3" if is_worker else "1",
            "zoneRequestsLimit": "3" if is_worker else "7",
        },
        "runtime": {"http": {}},
    }


def masked(text: str) -> str:
    return SECRET_PATTERN.sub("***", text)[:2000]


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
        raise RuntimeError(f"Yandex Cloud API HTTP {exc.code}: {masked(response_text)}") from exc


def wait_for_operation(operation: dict[str, object], *, token: str) -> None:
    operation_id = str(operation["id"])
    current = operation
    for _ in range(60):
        if current.get("error"):
            error = current["error"]
            raise RuntimeError(f"Yandex Cloud operation failed: {masked(json.dumps(error))}")
        if current.get("done"):
            print(f"Serverless revision deployed; operation {operation_id}")
            return
        time.sleep(2)
        encoded_id = urllib.parse.quote(operation_id, safe="")
        current = request_json(OPERATION_URL.format(encoded_id), token=token)
    raise TimeoutError(f"Yandex Cloud operation {operation_id} did not finish in 120 seconds")


def main() -> None:
    token = os.environ["IAM_TOKEN"]
    for component in ("api", "worker"):
        operation = request_json(
            DEPLOY_URL,
            token=token,
            method="POST",
            payload=build_payload(os.environ, component=component),
        )
        wait_for_operation(operation, token=token)


if __name__ == "__main__":
    main()
