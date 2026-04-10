"""
InfracostAnalyzer — runs the Infracost CLI against changed Terraform files
and returns a structured cost delta dict.

Requires:
  - `infracost` binary on PATH
  - INFRACOST_API_KEY env var (or prior `infracost auth login`)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


class InfracostAnalyzer:
    def __init__(self) -> None:
        self._binary = _find_infracost()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, terraform_files: list[str]) -> dict:
        """
        Run Infracost against the directories containing changed .tf files.
        Returns:
          {
            "projects": [...],
            "monthly_cost_delta_usd": float,
            "diff_summary": {...},
            "raw_output": str
          }
        """
        if not self._binary:
            return {"error": "infracost binary not found on PATH", "monthly_cost_delta_usd": 0.0}

        # Deduplicate directories
        tf_dirs = list({str(Path(f).parent) for f in terraform_files if Path(f).exists()})
        if not tf_dirs:
            return {"monthly_cost_delta_usd": 0.0, "projects": [], "diff_summary": {}}

        print(f"[Infracost] Analysing {len(tf_dirs)} Terraform director(ies): {tf_dirs}")

        with tempfile.TemporaryDirectory() as tmp:
            output_path = os.path.join(tmp, "infracost_output.json")
            result = self._run_infracost(tf_dirs, output_path)

        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_infracost(self, tf_dirs: list[str], output_path: str) -> dict:
        # Build --path arguments for each Terraform directory
        path_args: list[str] = []
        for d in tf_dirs:
            path_args += ["--path", d]

        cmd = [
            self._binary,
            "breakdown",
            *path_args,
            "--format", "json",
            "--show-skipped",
            "--out-file", output_path,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return {"error": "Infracost timed out after 120s", "monthly_cost_delta_usd": 0.0}
        except FileNotFoundError:
            return {"error": f"infracost binary not found: {self._binary}", "monthly_cost_delta_usd": 0.0}

        if proc.returncode not in (0, 1):  # 1 = non-zero cost, still valid output
            return {
                "error": f"Infracost exited with code {proc.returncode}: {proc.stderr[:500]}",
                "monthly_cost_delta_usd": 0.0,
            }

        try:
            with open(output_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return {"error": f"Failed to parse Infracost output: {exc}", "monthly_cost_delta_usd": 0.0}

        return _parse_infracost_output(data)


# ------------------------------------------------------------------
# Parsers
# ------------------------------------------------------------------

def _parse_infracost_output(data: dict) -> dict:
    projects = []
    for project in data.get("projects", []):
        diff = project.get("diff", {})
        breakdown = project.get("breakdown", {})
        projects.append({
            "name": project.get("name", ""),
            "path": project.get("metadata", {}).get("path", ""),
            "monthly_cost_usd": _safe_float(breakdown.get("totalMonthlyCost")),
            "monthly_cost_delta_usd": _safe_float(diff.get("totalMonthlyCost")),
            "resources_added": len(diff.get("resources", [])),
        })

    total_delta = sum(p["monthly_cost_delta_usd"] for p in projects)
    summary = data.get("summary", {})

    return {
        "projects": projects,
        "monthly_cost_delta_usd": round(total_delta, 2),
        "diff_summary": {
            "no_price_resources_count": summary.get("noPriceResourceCounts", {}),
            "total_detected_resources": summary.get("totalDetectedResources", 0),
            "total_supported_resources": summary.get("totalSupportedResources", 0),
        },
        "currency": data.get("currency", "USD"),
    }


def _safe_float(value: str | float | None) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _find_infracost() -> str | None:
    import shutil
    binary = shutil.which("infracost")
    if binary:
        return binary
    # Common install locations
    for candidate in ["/usr/local/bin/infracost", "/usr/bin/infracost", os.path.expanduser("~/.local/bin/infracost")]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    print("[Infracost] Warning: infracost binary not found. Terraform cost analysis will be skipped.")
    return None
