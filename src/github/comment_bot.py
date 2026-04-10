"""
CommentBot — posts or updates a GateKeeper analysis comment on a GitHub PR.

Strategy:
  - On the first run, create a new comment and store its ID in a workflow
    output / step summary so subsequent runs can find it.
  - On re-runs (same PR), search for an existing comment that starts with the
    GateKeeper header sentinel and update it in-place to avoid spam.
"""

from __future__ import annotations

from github import Github, GithubException

_COMMENT_SENTINEL = "<!-- gatekeeper-analysis -->"


class CommentBot:
    def __init__(self, token: str, repo_full_name: str, pr_number: int) -> None:
        self._gh = Github(token)
        self._repo = self._gh.get_repo(repo_full_name)
        self._pr = self._repo.get_pull(pr_number)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def post_or_update(self, verdict: dict) -> None:
        """Post a new comment or update the existing GateKeeper comment."""
        body = self._build_body(verdict)
        existing = self._find_existing_comment()

        if existing:
            print(f"[Bot] Updating existing comment #{existing.id}")
            try:
                existing.edit(body)
            except GithubException as exc:
                print(f"[Bot] Failed to update comment: {exc}")
        else:
            print("[Bot] Creating new PR comment")
            try:
                self._pr.create_issue_comment(body)
            except GithubException as exc:
                print(f"[Bot] Failed to create comment: {exc}")
                raise

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_body(self, verdict: dict) -> str:
        markdown = verdict.get("pr_comment_markdown", "")
        if not markdown:
            markdown = _minimal_markdown(verdict)
        # Prepend invisible sentinel so we can find the comment on re-runs
        return f"{_COMMENT_SENTINEL}\n{markdown}"

    def _find_existing_comment(self):
        """Return the first PR comment that starts with our sentinel, or None."""
        try:
            for comment in self._pr.get_issue_comments():
                if comment.body.startswith(_COMMENT_SENTINEL):
                    return comment
        except GithubException as exc:
            print(f"[Bot] Warning: could not list PR comments: {exc}")
        return None


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _minimal_markdown(verdict: dict) -> str:
    """Fallback comment body when pr_comment_markdown is missing."""
    v = verdict.get("verdict", "UNKNOWN")
    reason = verdict.get("verdict_reason", "No details available.")
    badges = {"BLOCK": "🔴 BLOCK", "WARN": "🟡 WARN", "APPROVE": "🟢 APPROVE"}
    badge = badges.get(v, f"⚪ {v}")

    actions = verdict.get("recommended_actions", [])
    actions_md = "\n".join(f"{i+1}. {a}" for i, a in enumerate(actions)) if actions else "_None_"

    return (
        f"## GateKeeper Analysis — {badge}\n\n"
        f"> {reason}\n\n"
        f"### Recommended Actions\n{actions_md}\n\n"
        "---\n*Powered by GateKeeper — Black Duck + AWS Pricing + Claude*"
    )
