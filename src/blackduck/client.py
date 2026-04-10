"""
Black Duck REST API client.

Authenticates with a bearer token, queries the KnowledgeBase API for each
component version, and returns a structured findings dict with CVE details,
CVSS scores, and license obligations.
"""

import asyncio
import hashlib
import json
import os
from typing import Any

import httpx


# Simple in-process cache keyed by (name, version) to avoid redundant API calls
_CACHE: dict[str, Any] = {}

_BD_TIMEOUT = 30  # seconds per request
_MAX_CONCURRENT = 5  # parallel BD lookups


class BlackDuckClient:
    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._bearer: str | None = None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _get_bearer(self, client: httpx.AsyncClient) -> str:
        if self._bearer:
            return self._bearer
        resp = await client.post(
            f"{self.base_url}/api/tokens/authenticate",
            headers={"Authorization": f"token {self.api_token}"},
            timeout=_BD_TIMEOUT,
        )
        resp.raise_for_status()
        self._bearer = resp.json()["bearerToken"]
        return self._bearer

    def _auth_headers(self, bearer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.blackducksoftware.component-detail-5+json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan_components(self, components: list[dict]) -> dict:
        """
        Scan a list of components (each with 'name', 'version', 'ecosystem').
        Returns:
          {
            "components": [<ComponentResult>, ...],
            "summary": {"critical": N, "high": N, "medium": N, "low": N}
          }
        """
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        async with httpx.AsyncClient(verify=True) as client:
            bearer = await self._get_bearer(client)
            tasks = [
                self._scan_one(client, bearer, semaphore, comp)
                for comp in components
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        component_results = []
        summary = {"critical": 0, "high": 0, "medium": 0, "low": 0}

        for comp, result in zip(components, results):
            if isinstance(result, Exception):
                print(f"[BD] Warning: scan failed for {comp['name']}@{comp.get('version','?')}: {result}")
                component_results.append({"component": comp, "vulnerabilities": [], "licenses": [], "error": str(result)})
                continue
            component_results.append(result)
            for vuln in result.get("vulnerabilities", []):
                sev = vuln.get("severity", "low").lower()
                if sev in summary:
                    summary[sev] += 1

        return {"components": component_results, "summary": summary}

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _scan_one(
        self,
        client: httpx.AsyncClient,
        bearer: str,
        semaphore: asyncio.Semaphore,
        component: dict,
    ) -> dict:
        cache_key = _cache_key(component)
        if cache_key in _CACHE:
            return _CACHE[cache_key]

        async with semaphore:
            result = await self._fetch_component(client, bearer, component)

        _CACHE[cache_key] = result
        return result

    async def _fetch_component(
        self, client: httpx.AsyncClient, bearer: str, component: dict
    ) -> dict:
        name = component["name"]
        version = component.get("version", "")
        ecosystem = component.get("ecosystem", "")

        # Search the KnowledgeBase for this component
        params = {
            "q": f"name:{name}",
            "limit": 5,
        }
        if ecosystem:
            params["q"] += f" packageManager:{ecosystem}"

        search_resp = await client.get(
            f"{self.base_url}/api/components",
            headers=self._auth_headers(bearer),
            params=params,
            timeout=_BD_TIMEOUT,
        )
        search_resp.raise_for_status()
        items = search_resp.json().get("items", [])

        if not items:
            return {"component": component, "vulnerabilities": [], "licenses": []}

        # Take the first match and fetch its version-level details
        comp_href = items[0].get("_meta", {}).get("href", "")
        if not comp_href:
            return {"component": component, "vulnerabilities": [], "licenses": []}

        # Fetch vulnerabilities for this component version
        vuln_url = f"{comp_href}/versions?q=versionName:{version}&limit=1"
        ver_resp = await client.get(
            vuln_url,
            headers=self._auth_headers(bearer),
            timeout=_BD_TIMEOUT,
        )
        ver_resp.raise_for_status()
        ver_items = ver_resp.json().get("items", [])

        if not ver_items:
            return {"component": component, "vulnerabilities": [], "licenses": []}

        ver_href = ver_items[0].get("_meta", {}).get("href", "")
        vulns = await self._fetch_vulnerabilities(client, bearer, ver_href)
        licenses = _extract_licenses(ver_items[0])

        return {
            "component": component,
            "vulnerabilities": vulns,
            "licenses": licenses,
        }

    async def _fetch_vulnerabilities(
        self, client: httpx.AsyncClient, bearer: str, version_href: str
    ) -> list[dict]:
        if not version_href:
            return []

        resp = await client.get(
            f"{version_href}/vulnerabilities",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Accept": "application/vnd.blackducksoftware.vulnerability-4+json",
            },
            timeout=_BD_TIMEOUT,
        )
        resp.raise_for_status()

        vulns = []
        for item in resp.json().get("items", []):
            vulns.append({
                "cve_id": item.get("name", ""),
                "description": item.get("description", ""),
                "severity": item.get("severity", ""),
                "cvss_score": item.get("cvss3", {}).get("baseScore") or item.get("cvss2", {}).get("baseScore"),
                "fix_version": item.get("remediationStatus", {}).get("remediationFixedAt", ""),
            })
        return vulns


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _cache_key(component: dict) -> str:
    raw = json.dumps(component, sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def _extract_licenses(version_item: dict) -> list[str]:
    license_info = version_item.get("license", {})
    if not license_info:
        return []
    # BD returns either a single license or a complex AND/OR tree
    if "licenseDisplay" in license_info:
        return [license_info["licenseDisplay"]]
    return [lic.get("name", "") for lic in license_info.get("licenses", [])]
