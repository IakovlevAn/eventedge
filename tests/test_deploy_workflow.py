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


def test_production_smoke_accounts_for_partial_moex_and_checks_new_routes() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    workflow = (repository_root / ".github/workflows/deploy.yml").read_text()

    assert workflow.count("for attempt in {1..6}; do") >= 5
    assert ".meta.requested == 20" in workflow
    assert ".meta.returned >= 12" in workflow
    assert '((.meta.returned + (.errors | length)) == .meta.requested)' in workflow
    assert '([.data[].ticker, .errors[].ticker] | unique | length) == 20' in workflow
    assert '"MARKET_DATA_UNAVAILABLE"' in workflow
    assert "Smoke test source registry and market events" in workflow
    assert '"${EVENTEDGE_API_URL}/v1/sources"' in workflow
    assert '"${EVENTEDGE_API_URL}/v1/events?limit=1"' in workflow


def test_trusted_auto_merge_accepts_codex_and_agent_branches() -> None:
    workflow = Path(".github/workflows/auto-merge.yml").read_text(encoding="utf-8")

    assert "startsWith(github.event.workflow_run.head_branch, 'codex/')" in workflow
    assert '"$head_ref" != codex/*' in workflow
    assert '"$head_ref" != agent/*' in workflow
    assert '"$head_repo" != "$REPOSITORY"' in workflow
    assert '"$base_ref" != "main"' in workflow
