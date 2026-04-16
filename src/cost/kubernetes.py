"""
KubernetesCostAnalyzer — extracts CPU/memory resource requests & limits from
Kubernetes manifests and estimates monthly cost delta using the Infracost
Cloud Pricing API (no AWS credentials required).
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path
from typing import Any

import httpx
import yaml


# Default region — overridable via env var
_AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")

_HOURS_PER_MONTH = 730
_INFRACOST_PRICING_URL = "https://pricing.api.infracost.io/graphql"

# Fallback rates (USD/hour) if Pricing API call fails
_FALLBACK_CPU_RATE_PER_VCPU_HOUR = 0.048   # ~m5 on-demand vCPU cost
_FALLBACK_MEM_RATE_PER_GB_HOUR = 0.006     # ~m5 on-demand memory cost

# Instance spec used to derive per-vCPU / per-GB rates
_BASELINE_INSTANCE = "m5.xlarge"
_BASELINE_VCPUS = 4
_BASELINE_MEM_GB = 16


class KubernetesCostAnalyzer:
    def __init__(self, infracost_api_key: str) -> None:
        self._infracost_api_key = infracost_api_key
        self._cpu_rate, self._mem_rate = self._fetch_rates()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, file_paths: list[str]) -> dict:
        """
        Parse K8s manifests, compute resource delta, and return cost estimate.
        Returns:
          {
            "workloads": [...],
            "monthly_cost_delta_usd": float,
            "summary": str
          }
        """
        workloads: list[dict] = []
        for path in file_paths:
            p = Path(path)
            if not p.exists():
                continue
            try:
                workloads.extend(_parse_manifest(p))
            except Exception as exc:
                print(f"[K8sCost] Failed to parse {path}: {exc}")

        total_cpu_delta = sum(w.get("cpu_request_delta_vcpu", 0) for w in workloads)
        total_mem_delta = sum(w.get("mem_request_delta_gb", 0) for w in workloads)

        monthly_cost_delta = (
            total_cpu_delta * self._cpu_rate * _HOURS_PER_MONTH
            + total_mem_delta * self._mem_rate * _HOURS_PER_MONTH
        )

        return {
            "workloads": workloads,
            "total_cpu_delta_vcpu": round(total_cpu_delta, 3),
            "total_mem_delta_gb": round(total_mem_delta, 3),
            "monthly_cost_delta_usd": round(monthly_cost_delta, 2),
            "pricing_region": _AWS_REGION,
            "summary": _build_summary(workloads, monthly_cost_delta),
        }

    # ------------------------------------------------------------------
    # Infracost Cloud Pricing API
    # ------------------------------------------------------------------

    def _fetch_rates(self) -> tuple[float, float]:
        """Fetch on-demand vCPU and memory rates via the Infracost Cloud Pricing API."""
        query = """
        query($filter: ProductFilter!) {
          products(filter: $filter) {
            prices(filter: {purchaseOption: "on_demand"}) {
              USD
            }
          }
        }
        """
        variables = {
            "filter": {
                "vendorName": "aws",
                "service": "AmazonEC2",
                "productFamily": "Compute Instance",
                "region": _AWS_REGION,
                "attributeFilters": [
                    {"key": "instanceType", "value": _BASELINE_INSTANCE},
                    {"key": "operatingSystem", "value": "Linux"},
                    {"key": "tenancy", "value": "Shared"},
                    {"key": "preInstalledSw", "value": "NA"},
                    {"key": "capacitystatus", "value": "Used"},
                ],
            }
        }

        try:
            resp = httpx.post(
                _INFRACOST_PRICING_URL,
                json={"query": query, "variables": variables},
                headers={
                    "X-Api-Key": self._infracost_api_key,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            products = data.get("data", {}).get("products", [])
            if not products or not products[0].get("prices"):
                raise ValueError("No pricing data returned from Infracost")

            hourly_price = float(products[0]["prices"][0]["USD"])
            cpu_rate = hourly_price / _BASELINE_VCPUS
            mem_rate = hourly_price / _BASELINE_MEM_GB
            print(
                f"[K8sCost] Infracost → {_BASELINE_INSTANCE} @ {_AWS_REGION}: "
                f"${hourly_price}/hr → CPU ${cpu_rate:.4f}/vCPU/hr, Mem ${mem_rate:.4f}/GB/hr"
            )
            return cpu_rate, mem_rate
        except Exception as exc:
            print(f"[K8sCost] Infracost Pricing API failed ({exc}), using fallback rates.")
            return _FALLBACK_CPU_RATE_PER_VCPU_HOUR, _FALLBACK_MEM_RATE_PER_GB_HOUR


# ------------------------------------------------------------------
# YAML parsing helpers
# ------------------------------------------------------------------

def _parse_manifest(path: Path) -> list[dict]:
    """Parse a YAML file (possibly multi-document) and extract workload resource data."""
    workloads: list[dict] = []
    text = path.read_text(encoding="utf-8")
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict):
            continue
        kind = doc.get("kind", "")
        if kind not in ("Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"):
            continue

        name = doc.get("metadata", {}).get("name", "unknown")
        spec = _get_pod_spec(doc, kind)
        if not spec:
            continue

        replicas = doc.get("spec", {}).get("replicas", 1) if kind != "DaemonSet" else 1
        containers = spec.get("containers", [])

        total_cpu_req = 0.0
        total_mem_req = 0.0
        total_cpu_lim = 0.0
        total_mem_lim = 0.0

        for container in containers:
            resources = container.get("resources", {})
            reqs = resources.get("requests", {})
            lims = resources.get("limits", {})
            total_cpu_req += _parse_cpu(reqs.get("cpu", "0"))
            total_mem_req += _parse_memory_gb(reqs.get("memory", "0"))
            total_cpu_lim += _parse_cpu(lims.get("cpu", "0"))
            total_mem_lim += _parse_memory_gb(lims.get("memory", "0"))

        workloads.append({
            "name": name,
            "kind": kind,
            "replicas": replicas,
            "source_file": str(path),
            "cpu_request_vcpu": round(total_cpu_req, 3),
            "mem_request_gb": round(total_mem_req, 3),
            "cpu_limit_vcpu": round(total_cpu_lim, 3),
            "mem_limit_gb": round(total_mem_lim, 3),
            # Delta = per-replica * replicas (treating entire workload as new/changed)
            "cpu_request_delta_vcpu": round(total_cpu_req * replicas, 3),
            "mem_request_delta_gb": round(total_mem_req * replicas, 3),
        })
    return workloads


def _get_pod_spec(doc: dict, kind: str) -> dict | None:
    spec = doc.get("spec", {})
    if kind == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
    if kind == "Pod":
        return spec
    return spec.get("template", {}).get("spec")


def _parse_cpu(value: str) -> float:
    """Convert Kubernetes CPU string to vCPU float. e.g. '500m' → 0.5, '2' → 2.0"""
    if not value or value == "0":
        return 0.0
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    try:
        return float(value)
    except ValueError:
        return 0.0


def _parse_memory_gb(value: str) -> float:
    """Convert Kubernetes memory string to GB float. e.g. '512Mi' → 0.512"""
    if not value or value == "0":
        return 0.0
    units = {
        "Ki": 1 / (1024 ** 2),
        "Mi": 1 / 1024,
        "Gi": 1.0,
        "Ti": 1024.0,
        "K": 1 / (1000 ** 2),
        "M": 1 / 1000,
        "G": 1.0,
        "T": 1000.0,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * multiplier
            except ValueError:
                return 0.0
    try:
        return float(value) / (1024 ** 3)  # assume bytes
    except ValueError:
        return 0.0



def _build_summary(workloads: list[dict], monthly_delta: float) -> str:
    if not workloads:
        return "No Kubernetes workloads detected in changed files."
    names = ", ".join(w["name"] for w in workloads[:5])
    suffix = f" (+{len(workloads) - 5} more)" if len(workloads) > 5 else ""
    sign = "+" if monthly_delta >= 0 else ""
    return (
        f"Detected {len(workloads)} workload(s): {names}{suffix}. "
        f"Estimated monthly cost delta: {sign}${monthly_delta:.2f} (ap-south-1, on-demand)."
    )
