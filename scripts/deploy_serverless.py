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
    materiality_mode = environment.get("NEWS_MATERIALITY_MODE", "disabled")
    if materiality_mode not in {"disabled", "shadow", "rank"}:
        raise ValueError(
            "NEWS_MATERIALITY_MODE must be disabled, shadow or rank"
        )
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
        "NEWS_MATERIALITY_MODE": materiality_mode,
        "NEWS_MATERIALITY_BATCH_LIMIT": "8",
        "EVENTEDGE_MONTHLY_BUDGET_RUB": "15000",
    }
    if admin_key := environment.get("EVENTEDGE_ADMIN_KEY"):
        runtime_environment["EVENTEDGE_ADMIN_KEY"] = admin_key
    return {
        "containerId": (
            environment["YC_WORKER_CONTAINER_ID"] if is_worker else environment["YC_CONTAINER_ID"]
        ),
        "description": f"GitHub {environment['DEPLOY_SHA']} ({component})",
        "resources": {
            # Both contours are I/O-bound. The latest production audit observed
            # worker peaks of 407 MiB and 11.5% CPU. 768 MiB retains 361 MiB of
            # memory headroom, while a 20% guaranteed CPU share trades latency
            # for lower processing cost. Persisted data, source coverage, YDB
            # throughput and LLM behaviour remain unchanged.
            "memory": "805306368",
            "cores": "1",
            "coreFraction": "20",
        },
        "executionTimeout": "180s",
        "serviceAccountId": environment["YC_RUNTIME_SERVICE_ACCOUNT_ID"],
        "imageSpec": {
            "imageUrl": environment["IMAGE_URL"],
            "environment": runtime_environment,
        },
        # The public API is allowed to scale to zero. This trades the first
        # request after an idle period for a cold-start delay and removes the
        # dominant always-on container charge. Seven concurrent requests still
        # cover one dashboard refresh plus burst reads after startup.
        "concurrency": "1" if is_worker else "7",
        "provisionPolicy": {"minInstances": "0"},
        "scalingPolicy": {
            # Peak allocation per zone is quota-safe: API 1x(1 CPU, 768 MiB)
            # plus worker 3x(1 CPU, 768 MiB) = 4 CPU and 3 GiB. Instance caps and
            # scale-to-zero control spend; YDB keeps snapshots consistent.
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
