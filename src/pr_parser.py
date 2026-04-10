"""
PRParser — extracts and categorises changed files from a GitHub PR diff.
Uses the local git checkout (Actions workspace) rather than the GitHub API
so it works without an extra API call and stays within rate limits.
"""

import subprocess
from pathlib import Path


# File extension / path patterns per category
_MANIFEST_PATTERNS = {
    "npm": ["package.json", "package-lock.json", "yarn.lock"],
    "pip": ["requirements.txt", "requirements-*.txt", "Pipfile", "Pipfile.lock", "pyproject.toml"],
    "maven": ["pom.xml"],
    "gradle": ["build.gradle", "build.gradle.kts", "settings.gradle"],
    "go": ["go.mod", "go.sum"],
    "ruby": ["Gemfile", "Gemfile.lock"],
    "nuget": ["*.csproj", "*.nuspec", "packages.config"],
}

_K8S_EXTENSIONS = {".yaml", ".yml"}
_K8S_PATH_KEYWORDS = ["k8s", "kubernetes", "helm", "charts", "manifests", "deploy"]

_TERRAFORM_EXTENSIONS = {".tf", ".tfvars"}


class PRParser:
    def __init__(self, repo_full_name: str, base_sha: str, head_sha: str) -> None:
        self.repo_full_name = repo_full_name
        self.base_sha = base_sha
        self.head_sha = head_sha

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_changed_files(self) -> list[str]:
        """Return list of files changed between base and head commits."""
        result = subprocess.run(
            ["git", "diff", "--name-only", self.base_sha, self.head_sha],
            capture_output=True,
            text=True,
            check=True,
        )
        files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
        return files

    def get_file_diff(self, file_path: str) -> str:
        """Return unified diff for a single file."""
        result = subprocess.run(
            ["git", "diff", self.base_sha, self.head_sha, "--", file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def filter_manifests(self, files: list[str]) -> list[str]:
        """Return files that are dependency manifests (package.json, pom.xml, etc.)."""
        matched: list[str] = []
        for f in files:
            path = Path(f)
            for patterns in _MANIFEST_PATTERNS.values():
                for pattern in patterns:
                    if path.match(pattern):
                        matched.append(f)
                        break
        return list(dict.fromkeys(matched))  # deduplicate, preserve order

    def filter_kubernetes(self, files: list[str]) -> list[str]:
        """Return files that are likely Kubernetes / Helm manifests."""
        matched: list[str] = []
        for f in files:
            path = Path(f)
            if path.suffix not in _K8S_EXTENSIONS:
                continue
            lower = f.lower()
            if any(kw in lower for kw in _K8S_PATH_KEYWORDS):
                matched.append(f)
                continue
            # Fallback: read first few bytes and check for Kubernetes API markers
            if path.exists() and _looks_like_k8s(path):
                matched.append(f)
        return matched

    def filter_terraform(self, files: list[str]) -> list[str]:
        """Return .tf and .tfvars files."""
        return [f for f in files if Path(f).suffix in _TERRAFORM_EXTENSIONS]

    def categorise(self, files: list[str]) -> dict[str, list[str]]:
        """Return a dict with all category → file lists in one call."""
        return {
            "manifests": self.filter_manifests(files),
            "kubernetes": self.filter_kubernetes(files),
            "terraform": self.filter_terraform(files),
            "other": [
                f for f in files
                if f not in self.filter_manifests(files)
                and f not in self.filter_kubernetes(files)
                and f not in self.filter_terraform(files)
            ],
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _looks_like_k8s(path: Path) -> bool:
    """Quick heuristic: look for 'apiVersion:' or 'kind:' in first 40 lines."""
    try:
        with path.open(errors="ignore") as fh:
            for i, line in enumerate(fh):
                if i > 40:
                    break
                stripped = line.strip()
                if stripped.startswith("apiVersion:") or stripped.startswith("kind:"):
                    return True
    except OSError:
        pass
    return False
