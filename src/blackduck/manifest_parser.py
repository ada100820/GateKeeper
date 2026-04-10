"""
ManifestParser — extracts dependency lists from common package manifests.

Supported formats:
  - package.json          (npm)
  - requirements.txt      (pip)
  - pom.xml               (Maven)
  - go.mod                (Go modules)
  - build.gradle          (Gradle — best-effort)
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path


class ManifestParser:
    def parse_all(self, file_paths: list[str]) -> list[dict]:
        """Parse all manifest files and return a flat list of component dicts."""
        components: list[dict] = []
        for path in file_paths:
            p = Path(path)
            if not p.exists():
                print(f"[ManifestParser] Skipping missing file: {path}")
                continue
            try:
                components.extend(self._parse_file(p))
            except Exception as exc:
                print(f"[ManifestParser] Failed to parse {path}: {exc}")
        # Deduplicate by (name, version, ecosystem)
        seen: set[tuple] = set()
        unique: list[dict] = []
        for c in components:
            key = (c["name"], c.get("version", ""), c.get("ecosystem", ""))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _parse_file(self, path: Path) -> list[dict]:
        name = path.name.lower()
        if name == "package.json":
            return self._parse_package_json(path)
        if name in ("requirements.txt",) or name.startswith("requirements-"):
            return self._parse_requirements_txt(path)
        if name == "pom.xml":
            return self._parse_pom_xml(path)
        if name == "go.mod":
            return self._parse_go_mod(path)
        if name in ("build.gradle", "build.gradle.kts"):
            return self._parse_gradle(path)
        return []

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_package_json(self, path: Path) -> list[dict]:
        data = json.loads(path.read_text(encoding="utf-8"))
        components: list[dict] = []
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            for name, version_spec in data.get(section, {}).items():
                components.append({
                    "name": name,
                    "version": _clean_npm_version(version_spec),
                    "ecosystem": "npm",
                    "source_file": str(path),
                    "dev": section != "dependencies",
                })
        return components

    def _parse_requirements_txt(self, path: Path) -> list[dict]:
        components: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            # Handle extras: package[extra]==1.0
            match = re.match(r"^([A-Za-z0-9_.\-]+)(?:\[.*?\])?(?:==|>=|<=|~=|!=|>|<)([^\s;]+)", line)
            if match:
                components.append({
                    "name": match.group(1),
                    "version": match.group(2),
                    "ecosystem": "pypi",
                    "source_file": str(path),
                    "dev": False,
                })
            else:
                # Bare package name without pinned version
                name = re.split(r"[>=<!;\[]", line)[0].strip()
                if name:
                    components.append({
                        "name": name,
                        "version": "",
                        "ecosystem": "pypi",
                        "source_file": str(path),
                        "dev": False,
                    })
        return components

    def _parse_pom_xml(self, path: Path) -> list[dict]:
        ns = {"m": "http://maven.apache.org/POM/4.0.0"}
        try:
            tree = ET.parse(path)
        except ET.ParseError as exc:
            raise ValueError(f"XML parse error: {exc}") from exc

        root = tree.getroot()
        components: list[dict] = []

        # Collect property variables for version substitution
        props: dict[str, str] = {}
        for prop in root.findall(".//m:properties/*", ns):
            props[prop.tag.split("}")[-1]] = prop.text or ""

        for dep in root.findall(".//m:dependency", ns):
            group_id = _xml_text(dep, "m:groupId", ns)
            artifact_id = _xml_text(dep, "m:artifactId", ns)
            version = _xml_text(dep, "m:version", ns)
            scope = _xml_text(dep, "m:scope", ns) or "compile"

            # Resolve ${property} references
            if version.startswith("${") and version.endswith("}"):
                prop_name = version[2:-1]
                version = props.get(prop_name, version)

            components.append({
                "name": f"{group_id}:{artifact_id}",
                "version": version,
                "ecosystem": "maven",
                "source_file": str(path),
                "dev": scope in ("test", "provided"),
            })
        return components

    def _parse_go_mod(self, path: Path) -> list[dict]:
        components: list[dict] = []
        in_require_block = False

        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped == "require (":
                in_require_block = True
                continue
            if in_require_block and stripped == ")":
                in_require_block = False
                continue
            if in_require_block or stripped.startswith("require "):
                content = stripped.removeprefix("require ").strip()
                # Skip indirect deps (still scan them but flag)
                indirect = "// indirect" in content
                content = content.replace("// indirect", "").strip()
                parts = content.split()
                if len(parts) >= 2:
                    components.append({
                        "name": parts[0],
                        "version": parts[1].lstrip("v"),
                        "ecosystem": "golang",
                        "source_file": str(path),
                        "dev": False,
                        "indirect": indirect,
                    })
        return components

    def _parse_gradle(self, path: Path) -> list[dict]:
        """Best-effort Gradle parser using regex (no full Groovy/Kotlin AST)."""
        components: list[dict] = []
        text = path.read_text(encoding="utf-8")
        # Matches: implementation 'group:artifact:version' or "group:artifact:version"
        pattern = re.compile(
            r"""(?:implementation|api|compileOnly|runtimeOnly|testImplementation)\s+['"]([^'"]+)['"]"""
        )
        for match in pattern.finditer(text):
            coord = match.group(1)
            parts = coord.split(":")
            if len(parts) >= 2:
                name = f"{parts[0]}:{parts[1]}"
                version = parts[2] if len(parts) > 2 else ""
                components.append({
                    "name": name,
                    "version": version,
                    "ecosystem": "maven",
                    "source_file": str(path),
                    "dev": False,
                })
        return components


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _clean_npm_version(spec: str) -> str:
    """Strip npm range operators to get a plain version string."""
    return re.sub(r"^[^0-9]*", "", spec).split(" ")[0] or spec


def _xml_text(element: ET.Element, tag: str, ns: dict) -> str:
    child = element.find(tag, ns)
    return (child.text or "").strip() if child is not None else ""
