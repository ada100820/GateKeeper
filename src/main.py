"""
GateKeeper — entry point.
Orchestrates: PR diff parsing → Black Duck scan → cost analysis → AI synthesis → PR comment.
"""

import asyncio
import json
import os
import sys

from pr_parser import PRParser
from blackduck.client import BlackDuckClient
from blackduck.manifest_parser import ManifestParser
from cost.kubernetes import KubernetesCostAnalyzer
from cost.infracost import InfracostAnalyzer
from ai.synthesizer import AISynthesizer
from github.comment_bot import CommentBot


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        print(f"[GateKeeper] ERROR: required environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return val


async def main() -> None:
    # --- collect required env vars ---
    github_token = _require_env("GITHUB_TOKEN")
    repo_full_name = _require_env("REPO_FULL_NAME")
    pr_number = int(_require_env("PR_NUMBER"))
    base_sha = _require_env("BASE_SHA")
    head_sha = _require_env("HEAD_SHA")

    bd_url = _require_env("BD_API_URL")
    bd_token = _require_env("BD_API_TOKEN")
    anthropic_key = _require_env("ANTHROPIC_API_KEY")

    print(f"[GateKeeper] Analysing PR #{pr_number} in {repo_full_name} ({base_sha[:7]}..{head_sha[:7]})")

    # --- 1. Parse PR diff ---
    parser = PRParser(repo_full_name=repo_full_name, base_sha=base_sha, head_sha=head_sha)
    changed_files = parser.get_changed_files()
    print(f"[GateKeeper] Changed files: {len(changed_files)}")

    manifest_files = parser.filter_manifests(changed_files)
    k8s_files = parser.filter_kubernetes(changed_files)
    terraform_files = parser.filter_terraform(changed_files)

    # --- 2. Black Duck scan (async) ---
    bd_task = asyncio.create_task(
        _run_blackduck(bd_url, bd_token, manifest_files)
    )

    # --- 3. Cost analysis (async, parallel with BD) ---
    cost_task = asyncio.create_task(
        _run_cost_analysis(k8s_files, terraform_files)
    )

    bd_findings, cost_findings = await asyncio.gather(bd_task, cost_task)

    # --- 4. AI synthesis ---
    synthesizer = AISynthesizer(api_key=anthropic_key)
    verdict = synthesizer.synthesize(bd_findings=bd_findings, cost_findings=cost_findings)
    print(f"[GateKeeper] Verdict: {verdict['verdict']}")

    # --- 5. Post PR comment ---
    bot = CommentBot(token=github_token, repo_full_name=repo_full_name, pr_number=pr_number)
    bot.post_or_update(verdict)

    # --- 6. Save report artifact ---
    report = {"verdict": verdict, "bd_findings": bd_findings, "cost_findings": cost_findings}
    with open("gatekeeper_report.json", "w") as f:
        json.dump(report, f, indent=2)

    print("[GateKeeper] Done.")

    # Exit non-zero if verdict is BLOCK so the workflow fails the PR check
    if verdict.get("verdict") == "BLOCK":
        sys.exit(2)


async def _run_blackduck(bd_url: str, bd_token: str, manifest_files: list[str]) -> dict:
    if not manifest_files:
        print("[BD] No manifest files changed — skipping Black Duck scan.")
        return {"components": [], "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0}}

    manifest_parser = ManifestParser()
    components = manifest_parser.parse_all(manifest_files)
    print(f"[BD] Parsed {len(components)} components from manifests.")

    client = BlackDuckClient(base_url=bd_url, api_token=bd_token)
    findings = await client.scan_components(components)
    return findings


async def _run_cost_analysis(k8s_files: list[str], terraform_files: list[str]) -> dict:
    results: dict = {"kubernetes": None, "terraform": None}

    if k8s_files:
        k8s_analyzer = KubernetesCostAnalyzer()
        results["kubernetes"] = k8s_analyzer.analyze(k8s_files)
    else:
        print("[Cost] No Kubernetes files changed.")

    if terraform_files:
        tf_analyzer = InfracostAnalyzer()
        results["terraform"] = tf_analyzer.analyze(terraform_files)
    else:
        print("[Cost] No Terraform files changed.")

    return results


if __name__ == "__main__":
    asyncio.run(main())
