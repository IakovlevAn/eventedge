from pathlib import Path


def test_schema_migration_runs_before_serverless_revision_deploy() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github/workflows/deploy.yml").read_text()

    build = workflow.index("- name: Build and push immutable image")
    migration = workflow.index("- name: Apply YDB schema migrations")
    deploy = workflow.index("- name: Deploy budget-capped serverless revision")

    assert build < migration < deploy
    assert '"$IMAGE_URL" \\\n            python -m eventedge.migrate' in workflow
    assert "YDB_ACCESS_TOKEN: ${{ steps.yandex-iam.outputs.token }}" in workflow


def test_production_smoke_retries_cold_evals_and_checks_new_routes() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github/workflows/deploy.yml").read_text()

    assert workflow.count("for attempt in {1..6}; do") >= 5
    assert "Smoke test source registry and market events" in workflow
    assert '"${EVENTEDGE_API_URL}/v1/sources"' in workflow
    assert '"${EVENTEDGE_API_URL}/v1/events?limit=1"' in workflow
