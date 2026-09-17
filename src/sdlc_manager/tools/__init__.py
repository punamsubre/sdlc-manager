"""SDLC Manager tool registration exports."""

from .github_tools import (
    github_create_pr,
    github_list_prs,
    github_merge_pr,
    github_pr_status,
)
from .lefthook_bridge import wire_repo_hooks
from .read_tools import (
    git_blame,
    git_branch_list,
    git_diff,
    git_log,
    git_remote_list,
    git_show_commit,
    git_stash_list,
    git_status,
)
from .review_tools import (
    generate_pr_description,
    get_quality_score,
    review_code,
    review_pr_diff,
)
from .terraform_tools import (
    terraform_fmt,
    terraform_init,
    terraform_plan,
    terraform_validate,
)
from .validation_tools import (
    apply_fixes,
    check_bigquery_schema,
    check_readiness,
    scan_secrets,
    validate_directory,
    validate_file,
    validate_sql_syntax,
)
from .write_tools import (
    git_add,
    git_checkout_branch,
    git_clone,
    git_commit,
    git_fetch,
    git_merge,
    git_pull,
    git_push,
    git_rebase,
    git_reset,
    git_stash,
    git_tag,
)

__all__ = [
    # Git Read
    "git_status",
    "git_log",
    "git_diff",
    "git_branch_list",
    "git_remote_list",
    "git_stash_list",
    "git_show_commit",
    "git_blame",
    # Git Write
    "git_clone",
    "git_checkout_branch",
    "git_add",
    "git_commit",
    "git_push",
    "git_pull",
    "git_merge",
    "git_stash",
    "git_rebase",
    "git_reset",
    "git_tag",
    "git_fetch",
    "wire_repo_hooks",
    # GitHub
    "github_create_pr",
    "github_list_prs",
    "github_pr_status",
    "github_merge_pr",
    # Terraform
    "terraform_init",
    "terraform_validate",
    "terraform_fmt",
    "terraform_plan",
    # Validation
    "scan_secrets",
    "check_bigquery_schema",
    "validate_file",
    "validate_sql_syntax",
    "validate_directory",
    "apply_fixes",
    "check_readiness",
    # AI Review
    "review_code",
    "get_quality_score",
    "review_pr_diff",
    "generate_pr_description",
]
