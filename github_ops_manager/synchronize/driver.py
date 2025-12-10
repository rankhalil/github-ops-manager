"""Orchestrates the synchronization of GitHub objects."""

import asyncio
import time
from pathlib import Path

import structlog

from github_ops_manager.configuration.models import GitHubAuthenticationType
from github_ops_manager.github.adapter import GitHubKitAdapter
from github_ops_manager.processing.yaml_processor import YAMLProcessingError, YAMLProcessor
from github_ops_manager.synchronize.issues import render_issue_bodies, sync_github_issues
from github_ops_manager.synchronize.pull_requests import sync_github_pull_requests
from github_ops_manager.synchronize.results import AllIssueSynchronizationResults, ProcessIssuesResult

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


async def run_process_issues_workflow(
    repo: str,
    github_pat_token: str | None,
    github_app_id: int | None,
    github_app_private_key_path: Path | None,
    github_app_installation_id: int | None,
    github_auth_type: GitHubAuthenticationType,
    github_api_url: str,
    yaml_path: Path,
    raise_on_yaml_error: bool = False,
    testing_as_code_workflow: bool = False,
    additional_labels: list[str] | None = None,
) -> ProcessIssuesResult:
    """Run the process-issues workflow: load issues from YAML and return them/errors."""
    processor = YAMLProcessor(raise_on_error=raise_on_yaml_error)
    try:
        issues_model = processor.load_issues_model([str(yaml_path)])
    except YAMLProcessingError as e:
        return ProcessIssuesResult(AllIssueSynchronizationResults([], [], 0), errors=e.errors)

    # Render Jinja2 templates for issue bodies if provided.
    if issues_model.issue_template:
        issues_model = await render_issue_bodies(issues_model)

    # Merge additional labels from CLI if provided.
    if additional_labels:
        logger.info("Merging additional labels from CLI with YAML labels", additional_labels=additional_labels)
        for issue in issues_model.issues:
            yaml_labels = issue.labels or []
            # Combine and deduplicate: YAML labels + CLI labels
            merged_labels = list(set(yaml_labels + additional_labels))
            issue.labels = merged_labels
            logger.debug("Merged labels for issue", issue_title=issue.title, merged_labels=merged_labels)

    # Set up GitHub adapter.
    github_adapter = await GitHubKitAdapter.create(
        repo=repo,
        github_auth_type=github_auth_type,
        github_pat_token=github_pat_token,
        github_app_id=github_app_id,
        github_app_private_key_path=github_app_private_key_path,
        github_app_installation_id=github_app_installation_id,
        github_api_url=github_api_url,
    )

    # Synchronize issues to GitHub.
    start_time = time.time()
    logger.info("Processing issues", start_time=start_time)
    issue_sync_results = await sync_github_issues(issues_model.issues, github_adapter)
    end_time = time.time()
    total_time = end_time - start_time
    logger.info(
        "Processed issues",
        start_time=start_time,
        end_time=end_time,
        duration=round(total_time, 2),
        desired_issue_count=len(issues_model.issues),
        issue_sync_result_count=len(issue_sync_results.results),
    )

    # Synchronize pull requests for issues that specify a pull_request field.
    repo_info = await github_adapter.get_repository()
    default_branch = repo_info.default_branch

    # Refresh issues so that if any new issues were created, they're picked up
    # as part of Pull Request logic. This also helps us identify when the
    # GitHub API has become eventually consistent with the new issues we
    # may have created.
    logger.info("Waiting for GitHub API to update with new issues")
    max_wait_time = 120
    refresh_start_time = time.time()
    while time.time() - refresh_start_time < max_wait_time:
        refreshed_issues = await github_adapter.list_issues()
        if len(refreshed_issues) == issue_sync_results.expected_number_of_github_issues_after_sync:
            break
        logger.info(
            "GitHub API currently has fewer issues than expected",
            expected=issue_sync_results.expected_number_of_github_issues_after_sync,
            actual=len(refreshed_issues),
        )
        await asyncio.sleep(3)
    else:
        logger.warning("GitHub API did not update with new issues in time", max_wait_time=max_wait_time)
        raise RuntimeError("GitHub API did not update with new issues in time")
    logger.info("GitHub API updated with new issues", duration=round(time.time() - refresh_start_time, 2))

    # Fetch content of all existing pull requests. This requires us to fetch
    # a simple list of pull requests, then fetch the content of each pull request.
    logger.info("Refreshing pull requests from GitHub")
    refresh_simple_pull_request_start_time = time.time()
    existing_simple_pull_requests = await github_adapter.list_pull_requests()
    refresh_simple_pull_request_end_time = time.time()
    refresh_simple_pull_request_duration = refresh_simple_pull_request_end_time - refresh_simple_pull_request_start_time
    logger.info("Refreshed simple pull requests from GitHub", duration=round(refresh_simple_pull_request_duration, 2))

    refresh_pull_request_start_time = time.time()
    existing_pull_requests = [await github_adapter.get_pull_request(pr.number) for pr in existing_simple_pull_requests]
    refresh_pull_request_end_time = time.time()
    refresh_pull_request_duration = refresh_pull_request_end_time - refresh_pull_request_start_time
    logger.info("Refreshed pull requests from GitHub", duration=round(refresh_pull_request_duration, 2))
    refresh_end_time = time.time()
    refresh_duration = refresh_end_time - refresh_start_time
    logger.info("Refreshed all GitHub resources", duration=round(refresh_duration, 2))

    # Identify directory that YAML file is in. All files attached to pull
    # requests will be relative to this directory.
    yaml_dir = yaml_path.parent
    logger.info("YAML directory where pull request files are relative to", yaml_dir=str(yaml_dir), original_yaml_path=str(yaml_path))

    start_time = time.time()
    logger.info("Processing pull requests", start_time=start_time)
    await sync_github_pull_requests(
        issues_model.issues,
        refreshed_issues,
        existing_pull_requests,
        github_adapter,
        default_branch,
        yaml_dir,
        testing_as_code_workflow=testing_as_code_workflow,
    )
    end_time = time.time()
    total_time = end_time - start_time
    logger.info("Processed pull requests", start_time=start_time, end_time=end_time, duration=round(total_time, 2))
    return ProcessIssuesResult(issue_sync_results)
