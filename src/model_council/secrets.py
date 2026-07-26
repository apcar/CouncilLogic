from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class SecretResolver(Protocol):
    def resolve(self, name: str) -> str | None:
        """Return a secret value without logging, printing, or persisting it."""

    def source_for(self, name: str) -> str | None:
        """Return a non-sensitive source description when the secret is available."""


@dataclass(frozen=True)
class EnvironmentSecretResolver:
    environ: dict[str, str] | os._Environ[str] = field(
        default_factory=lambda: os.environ
    )

    def resolve(self, name: str) -> str | None:
        value = self.environ.get(name)
        return value if value else None

    def source_for(self, name: str) -> str | None:
        return "process environment" if self.environ.get(name) else None


@dataclass(frozen=True)
class CommandSecretResolver:
    command: tuple[str, ...]
    timeout_seconds: float = 10.0

    @classmethod
    def from_string(cls, command: str) -> CommandSecretResolver:
        parts = tuple(shlex.split(command))
        if not parts:
            raise ValueError("Secret command cannot be empty")
        executable = Path(parts[0]).expanduser()
        if not executable.is_absolute():
            raise ValueError("Secret command executable must be an absolute path")
        return cls((str(executable), *parts[1:]))

    def resolve(self, name: str) -> str | None:
        try:
            completed = subprocess.run(
                [*self.command, name],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout.rstrip("\r\n")
        return value if value else None

    def source_for(self, name: str) -> str | None:
        return "external secret command" if self.resolve(name) else None


@dataclass(frozen=True)
class ChainedSecretResolver:
    resolvers: tuple[SecretResolver, ...]

    def resolve(self, name: str) -> str | None:
        for resolver in self.resolvers:
            value = resolver.resolve(name)
            if value:
                return value
        return None

    def source_for(self, name: str) -> str | None:
        for resolver in self.resolvers:
            source = resolver.source_for(name)
            if source:
                return source
        return None


def default_secret_resolver(
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> SecretResolver:
    resolvers: list[SecretResolver] = []
    command = environ.get("MODEL_COUNCIL_SECRET_COMMAND")
    if command:
        resolvers.append(CommandSecretResolver.from_string(command))
    resolvers.append(EnvironmentSecretResolver(environ))
    return ChainedSecretResolver(tuple(resolvers))
