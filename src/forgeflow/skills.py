"""Trusted, dynamically loaded Agent Skills; Markdown cannot grant authority."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath

import yaml

from .contracts import DomainError, SkillSnapshot

ROLE_TOOLS: dict[str, frozenset[str]] = {
    "supervisor": frozenset({"knowledge.search", "memory.search"}),
    "pm": frozenset({"knowledge.search", "memory.search"}),
    "hardware": frozenset({"knowledge.search", "memory.search", "sandbox.execute"}),
}


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("Duplicate or non-string frontmatter key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


class SkillRegistry:
    NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    MAX_SKILL_BYTES = 64 * 1024
    MAX_RESOURCE_BYTES = 256 * 1024

    def __init__(
        self,
        trustedroot: str | Path,
        role_tools: dict[str, frozenset[str]] | None = None,
    ):
        root = Path(trustedroot)
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise DomainError(
                "INVALID_REGISTRY",
                "Trusted registry must be an absolute regular directory",
                422,
            )
        self.root = root.resolve(strict=True)
        # Constructor configuration is trusted control-plane policy, never skill metadata.
        self.role_tools = {
            role: frozenset(tools)
            for role, tools in (ROLE_TOOLS if role_tools is None else role_tools).items()
        }

    def _path(self, name: str, resource: str = "SKILL.md") -> Path:
        if not isinstance(name, str) or len(name) > 64 or not self.NAME.fullmatch(name):
            raise DomainError("INVALID_SKILL", "Skill name invalid", 422)
        if not isinstance(resource, str):
            raise DomainError("INVALID_RESOURCE", "Resource must be a string", 422)
        path = PurePosixPath(resource)
        if (
            "\\" in resource
            or path.is_absolute()
            or not path.parts
            or any(part in {".", ".."} for part in resource.split("/"))
        ):
            raise DomainError("INVALID_RESOURCE", "Resource must remain within its skill", 422)
        current = self.root
        for part in (name, *path.parts):
            current = current / part
            if current.is_symlink():
                raise DomainError("INVALID_RESOURCE", "Symlink resources are rejected", 422)
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(self.root / name)
        except (ValueError, FileNotFoundError, OSError) as exc:
            raise DomainError("INVALID_RESOURCE", "Resource unavailable", 404) from exc
        return resolved

    def _read(self, path: Path, limit: int) -> bytes:
        try:
            # Nonblocking open prevents a FIFO from hanging before fstat rejects it.
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
                    raise DomainError(
                        "INVALID_RESOURCE",
                        "Resource must be a bounded regular file",
                        422,
                    )
                data = source.read(limit + 1)
        except OSError as exc:
            raise DomainError("INVALID_RESOURCE", "Resource unavailable", 404) from exc
        if len(data) > limit:
            raise DomainError("RESOURCE_TOO_LARGE", "Resource exceeds limit", 422)
        return data

    def load(self, name: str, role: str) -> SkillSnapshot:
        if role not in self.role_tools:
            raise DomainError("ROLE_DENIED", "Role has no registered skill capabilities", 403)
        data = self._read(self._path(name), self.MAX_SKILL_BYTES)
        try:
            text = data.decode("utf-8")
            lines = text.splitlines()
            if not lines or lines[0] != "---":
                raise ValueError("Missing frontmatter")
            end = lines.index("---", 1)
            metadata = yaml.load("\n".join(lines[1:end]), Loader=_UniqueLoader)
            if not isinstance(metadata, dict) or metadata.get("name") != name:
                raise ValueError("Frontmatter name differs from directory")
            description = metadata.get("description")
            if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
                raise ValueError("Description missing or invalid")
            requested = metadata.get("allowed-tools", "")
            if isinstance(requested, str):
                tools = set(requested.split())
            elif isinstance(requested, list) and all(isinstance(item, str) for item in requested):
                tools = set(requested)
            else:
                raise ValueError("allowed-tools must be a string or list")
            metadata_extra = metadata.get("metadata", {})
            if not isinstance(metadata_extra, dict):
                raise TypeError("metadata must be a mapping")
            sha256 = hashlib.sha256(data).hexdigest()
            version = metadata_extra.get("version", sha256)
            if not isinstance(version, str) or not version or len(version) > 128:
                raise ValueError("Version invalid")
        except (UnicodeDecodeError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise DomainError("INVALID_SKILL", "Skill frontmatter invalid", 422) from exc
        return SkillSnapshot(
            name=name,
            role=role,
            version=version,
            content=text,
            sha256=sha256,
            allowed_tools=tuple(sorted(tools & self.role_tools[role])),
        )

    def resource(self, name: str, relative_path: str, role: str) -> dict:
        """Progressive resource read with hash; callers must never host-execute it."""
        self.load(name, role)  # Revalidates registry/role each read, no stale authority cache.
        if relative_path.split("/")[0] not in {"references", "assets", "scripts"}:
            raise DomainError("INVALID_RESOURCE", "Use a registered skill resource directory", 422)
        data = self._read(self._path(name, relative_path), self.MAX_RESOURCE_BYTES)
        return {
            "path": relative_path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "content": data,
            "executable_on_host": False,
        }
