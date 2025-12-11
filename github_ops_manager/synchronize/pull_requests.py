"""Contains logic for synchronizing pull requests."""

import re
from pathlib import Path

import structlog
from githubkit.versions.latest.models import Issue, PullRequest
from structlog.contextvars import bound_contextvars

from github_ops_manager.github.adapter import GitHubKitAdapter
from github_ops_manager.schemas.default_issue import IssueModel, PullRequestModel
from github_ops_manager.synchronize.models import SyncDecision
from github_ops_manager.synchronize.utils import compare_github_field, compare_label_sets
from github_ops_manager.utils.helpers import generate_branch_name

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


async def pull_request_has_closing_keywords(issue_number: int, pull_request_body: str | None) -> bool:
    """Check if a pull request has closing keywords that reference an issue number."""
    logger.debug(
        "Analyzing pull request body for closing keywords",
        issue_number=issue_number,
        pull_request_body=pull_request_body,
    )
    closing_keywords = [
        "close",
        "closes",
        "closed",
        "fix",
        "fixes",
        "fixed",
        "resolve",
        "resolves",
        "resolved",
    ]
    if pull_request_body is None:
        logger.debug("Pull request body is None, so it cannot have closing keywords")
        return False

    for keyword in closing_keywords:
        # Create regex pattern that matches:
        # - Word boundary before keyword (\b)
        # - The keyword
        # - Word boundary after keyword (\b)
        # - Flexible whitespace (\s+)
        # - Hash symbol (#)
        # - Exact issue number
        # - Word boundary after issue number (\b)
        pattern = rf"\b{re.escape(keyword)}\s+#{issue_number}\b"

        if re.search(pattern, pull_request_body, re.IGNORECASE):
            logger.debug("Pull request body contains closing keyword", keyword=keyword, issue_number=issue_number, pattern=pattern)
            return True

    logger.debug("Pull request body does not contain any closing keywords", issue_number=issue_number)
    return False


async def get_pull_request_associated_with_issue(issue: Issue, existing_pull_requests: list[PullRequest]) -> PullRequest | None:
    """Get the pull request associated with the issue.

    The only way to do this is to parse the body of the pull request and look
    for closing keywords that reference the issue number. This is extremely
    hacky; it's also the only reasonable way to do this, as GitHub does not
    have an API for directly getting the pull request associated with an issue.
    """
    for pr in existing_pull_requests:
        logger.debug("Checking if pull request has closing keywords relating to issue", issue_number=issue.number, pull_request_body=pr.body)
        if await pull_request_has_closing_keywords(issue.number, pr.body):
            logger.debug("Pull request has closing keywords relating to issue", issue_number=issue.number, pull_request_body=pr.body)
            return pr
    logger.debug("No pull request has closing keywords relating to issue", issue_number=issue.number)
    return None


async def get_desired_pull_request_file_content(base_directory: Path, desired_issue: IssueModel) -> list[tuple[str, str]]:
    """Get the content of the desired pull request files."""
    if desired_issue.pull_request is None:
        raise ValueError("Desired issue has no pull request associated with it")
    files: list[tuple[str, str]] = []
    for file in desired_issue.pull_request.files:
        file_path = base_directory / file
        logger.info("Checking if file exists", file=file, file_path=str(file_path), base_directory=str(base_directory))
        if file_path.exists():
            files.append((file, file_path.read_text(encoding="utf-8")))
        else:
            logger.warning("Pull Request file not found", file=file, issue_title=desired_issue.title)
    return files


async def decide_github_pull_request_file_sync_action(
    desired_file_data: list[tuple[str, str]],
    github_pull_request: PullRequest,
    github_adapter: GitHubKitAdapter,
) -> SyncDecision:
    """Compare a YAML pull request's file and a GitHub pull request, and decide whether to create, update, or no-op."""
    current_files = await github_adapter.list_files_in_pull_request(github_pull_request.number)
    for desired_file_name, desired_file_content in desired_file_data:
        for current_file in current_files:
            if current_file.filename == desired_file_name:
                github_file_content = await github_adapter.get_file_content_from_pull_request(
                    file_path=desired_file_name,
                    branch=github_pull_request.head.ref,
                )
                if github_file_content == desired_file_content:
                    return SyncDecision.NOOP
                else:
                    return SyncDecision.UPDATE
    return SyncDecision.CREATE


async def decide_github_pull_request_sync_action(desired_issue: IssueModel, existing_pull_request: PullRequest | None = None) -> SyncDecision:
    """Compare a YAML issue and a GitHub issue, and decide whether to create, update, or no-op the associated pull request."""
    if desired_issue.pull_request is None:
        raise ValueError("Desired issue has no pull request associated with it")

    if existing_pull_request is None:
        logger.info("Existing issue has no pull request linked to it, creating a new one", issue_title=desired_issue.title)
        return SyncDecision.CREATE

    # Compare relevant pull request fields
    pr_fields_to_compare = ["title", "body"]
    for field in pr_fields_to_compare:
        desired_value = getattr(desired_issue.pull_request, field, None)
        github_value = getattr(existing_pull_request, field, None)
        field_decision = await compare_github_field(desired_value, github_value)
        if field_decision == SyncDecision.UPDATE or field_decision == SyncDecision.CREATE:
            logger.info(
                "Pull request needs to be updated",
                issue_title=desired_issue.title,
                pr_field=field,
                current_value=github_value,
                new_value=desired_value,
            )
            return SyncDecision.UPDATE

    # Next, check the labels of the existing and desired pull request
    decision = await compare_label_sets(desired_issue.pull_request.labels, getattr(existing_pull_request, "labels", []))
    if decision == SyncDecision.UPDATE:
        logger.info(
            "Existing pull request labels do not match desired labels, updating the pull request",
            issue_title=desired_issue.title,
        )
        return SyncDecision.UPDATE

    logger.info(
        "Pull request is up to date",
        issue_title=desired_issue.title,
        pr_number=existing_pull_request.number,
        pr_title=existing_pull_request.title,
        pr_body=existing_pull_request.body,
    )
    return SyncDecision.NOOP


async def commit_files_to_branch(
    desired_issue: IssueModel, existing_issue: Issue, desired_branch_name: str, base_directory: Path, github_adapter: GitHubKitAdapter
) -> None:
    """Commit files to a branch."""
    if desired_issue.pull_request is None:
        raise ValueError("Desired issue has no pull request associated with it")

    files_to_commit: list[tuple[str, str]] = []
    missing_files: list[str] = []
    logger.info("Preparing files to commit for pull request", issue_title=desired_issue.title, branch=desired_branch_name)
    for file_path in desired_issue.pull_request.files:
        try:
            files_to_commit = await get_desired_pull_request_file_content(base_directory, desired_issue)
        except FileNotFoundError as exc:
            logger.error("File for PR not found or unreadable", file=file_path, error=str(exc))
            missing_files.append(file_path)
    if missing_files:
        logger.error("Skipping PR creation due to missing files", files=missing_files, issue_title=desired_issue.title)
        raise ValueError("Skipping PR creation due to missing files")

    # Commit files to branch
    logger.info("Committing files to branch", issue_title=desired_issue.title, branch=desired_branch_name)
    commit_message = (
        f"chore: attach files for PR '{desired_issue.pull_request.title}' associated with issue '{desired_issue.title}' ({existing_issue.number})"
    )
    await github_adapter.commit_files_to_branch(desired_branch_name, files_to_commit, commit_message)


async def sync_github_pull_request(
    desired_issue: IssueModel,
    existing_issue: Issue,
    github_adapter: GitHubKitAdapter,
    default_branch: str,
    base_directory: Path,
    existing_pull_request: PullRequest | None = None,
    testing_as_code_workflow: bool = False,
    create_branches: bool = False,
) -> None:
    """Synchronize a specific pull request for an issue."""
    with bound_contextvars(
        desired_issue_title=desired_issue.title,
        existing_issue_title=existing_issue.title,
        existing_issue_number=existing_issue.number,
        existing_pull_request_number=existing_pull_request.number if existing_pull_request is not None else "None",
        existing_pull_request_title=existing_pull_request.title if existing_pull_request is not None else "None",
        existing_pull_request_body=existing_pull_request.body if existing_pull_request is not None else "None",
    ):
        # Ignoring type below because we know that the pull_request field is
        # not None at this point.
        pr: PullRequestModel = desired_issue.pull_request  # type: ignore
        if testing_as_code_workflow is True:
            pr.body = (
                f"**Quicksilver**: Automatically generated Pull Request for "
                f"issue #{existing_issue.number}, {existing_issue.title}. "
                f"Closes #{existing_issue.number}"
            )

        if pr.body is None:
            pr.body = f"Closes #{existing_issue.number}"
        pr_labels = pr.labels or []

        # Determine branch name
        desired_branch_name = pr.branch or generate_branch_name(existing_issue.number, desired_issue.title)

        # Ensure that pull request body has closing keywords. If it doesn't,
        # then we need to add them to the bottom of the body.
        if not await pull_request_has_closing_keywords(existing_issue.number, pr.body):
            pr.body = f"{pr.body}\n\nCloses #{existing_issue.number}"

        logger.info("Pull request body", body=pr.body, existing_issue_number=existing_issue.number, existing_issue_title=existing_issue.title)

        # Make overall PR sync decision
        pr_sync_decision = await decide_github_pull_request_sync_action(desired_issue, existing_pull_request=existing_pull_request)
        if pr_sync_decision == SyncDecision.CREATE:
            # Check if branch exists, create if not
            if not await github_adapter.branch_exists(desired_branch_name):
                if create_branches:
                    logger.info("Creating branch for PR", branch=desired_branch_name, base_branch=default_branch)
                    await github_adapter.create_branch(desired_branch_name, default_branch)
                else:
                    logger.warning(
                        "Branch does not exist and create_branches=False, skipping PR creation",
                        branch=desired_branch_name,
                        issue_title=desired_issue.title,
                    )
                    return
            else:
                logger.info("Branch already exists, skipping creation", branch=desired_branch_name)

            # Commit files to branch
            await commit_files_to_branch(desired_issue, existing_issue, desired_branch_name, base_directory, github_adapter)

            logger.info("Creating new PR for issue", branch=desired_branch_name, base_branch=default_branch)
            new_pr = await github_adapter.create_pull_request(
                title=pr.title,
                head=desired_branch_name,
                base=default_branch,
                body=pr.body,
            )
            logger.info("Created new PR", pr_number=new_pr.number, branch=desired_branch_name)
            await github_adapter.set_labels_on_issue(new_pr.number, pr_labels)
            logger.info("Set labels on new PR", pr_number=new_pr.number, labels=pr_labels)
        elif pr_sync_decision == SyncDecision.UPDATE:
            if existing_pull_request is None:
                raise ValueError("Existing pull request not found")
            logger.info("Updating existing PR for issue", pr_number=existing_pull_request.number)
            await github_adapter.update_pull_request(
                pull_number=existing_pull_request.number,
                title=pr.title,
                body=pr.body,
            )
            await github_adapter.set_labels_on_issue(existing_pull_request.number, pr_labels)
            desired_file_data = await get_desired_pull_request_file_content(base_directory, desired_issue)
            pr_file_sync_decision = await decide_github_pull_request_file_sync_action(desired_file_data, existing_pull_request, github_adapter)
            if pr_file_sync_decision == SyncDecision.CREATE:
                # The branch will already exist, so we don't need to create it.
                # However, we do need to commit the files to the branch.
                await commit_files_to_branch(desired_issue, existing_issue, desired_branch_name, base_directory, github_adapter)


async def sync_github_pull_requests(
    desired_issues: list[IssueModel],
    existing_issues: list[Issue],
    existing_pull_requests: list[PullRequest],
    github_adapter: GitHubKitAdapter,
    default_branch: str,
    base_directory: Path,
    testing_as_code_workflow: bool = False,
    create_prs: bool = False,
    create_branches: bool = False,
) -> None:
    """Process pull requests for issues that specify a pull_request field."""
    # Guard clause: Early exit if create_prs is False
    if not create_prs:
        logger.info("Skipping pull request processing because create_prs=False")
        return

    desired_issues_with_prs = [issue for issue in desired_issues if issue.pull_request is not None]
    for desired_issue in desired_issues_with_prs:
        existing_issue = next((issue for issue in existing_issues if issue.title == desired_issue.title), None)
        if existing_issue is not None:
            logger.info(
                "Existing issue found",
                desired_issue_title=desired_issue.title,
                existing_issue_title=existing_issue.title,
                existing_issue_number=existing_issue.number,
            )
        else:
            logger.error("Desired issue not found in existing issues", desired_issue_title=desired_issue.title)
            continue

        # Find existing PR associated with existing issue, if any.
        existing_pr = await get_pull_request_associated_with_issue(existing_issue, existing_pull_requests)

        await sync_github_pull_request(
            desired_issue,
            existing_issue,
            github_adapter,
            default_branch,
            base_directory,
            existing_pull_request=existing_pr,
            testing_as_code_workflow=testing_as_code_workflow,
            create_branches=create_branches,
        )
