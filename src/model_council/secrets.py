from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol


Runner = Callable[..., "subprocess.CompletedProcess[str]"]
REDACTED = "[REDACTED]"


def redact(text: str, *secrets: str | None) -> str:
    """Replace every non-trivial secret value inside ``text``."""

    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text


def _minimal_env(*extra_prefixes: str) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    for key in ("HOME", "USER", "LOGNAME", "TMPDIR"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in os.environ.items():
        if any(key.startswith(prefix) for prefix in extra_prefixes):
            env[key] = value
    return env


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


_KEYCHAIN_MDAT_RE = re.compile(r'"mdat"<timedate>=(?:0x[0-9A-Fa-f]+\s+)?"(\d{14})Z')
_OP_REF_RE = re.compile(r"^op://[^\s/]+/[^\s/]+(/[^\s/]+){1,2}$")


@dataclass(frozen=True)
class KeychainSecretResolver:
    """Read keys from the macOS login Keychain via ``security``.

    Each key is a generic password with service ``service`` and account equal
    to the secret name (for example ``OPENAI_API_KEY``). Values are read with
    ``-w`` into memory only. The item's modification date gives the key age.
    """

    service: str = "councillogic"
    executable: str = "/usr/bin/security"
    timeout_seconds: float = 10.0
    runner: Runner = field(default=subprocess.run, repr=False, compare=False)

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str] | None:
        try:
            return self.runner(
                [self.executable, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_minimal_env(),
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def resolve(self, name: str) -> str | None:
        completed = self._run(
            ["find-generic-password", "-s", self.service, "-a", name, "-w"]
        )
        if completed is None or completed.returncode != 0:
            return None
        value = completed.stdout.rstrip("\r\n")
        return value if value else None

    def source_for(self, name: str) -> str | None:
        return (
            f"macOS Keychain (service {self.service})"
            if self.resolve(name)
            else None
        )

    def updated_at(self, name: str) -> datetime | None:
        completed = self._run(
            ["find-generic-password", "-s", self.service, "-a", name]
        )
        if completed is None or completed.returncode != 0:
            return None
        # Attributes go to stdout; never inspect a password line here.
        match = _KEYCHAIN_MDAT_RE.search(completed.stdout + completed.stderr)
        if not match:
            return None
        return datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )


@dataclass(frozen=True)
class OnePasswordSecretResolver:
    """Read keys from 1Password with ``op read op://vault/item/field``.

    The reference (not the key) comes from ``<SECRET_NAME>_OP_REF`` in the
    environment, for example ``OPENAI_API_KEY_OP_REF=op://AI/OpenAI/credential``.
    """

    environ: dict[str, str] | os._Environ[str] = field(
        default_factory=lambda: os.environ
    )
    executable: str | None = None
    timeout_seconds: float = 20.0
    runner: Runner = field(default=subprocess.run, repr=False, compare=False)

    def reference_for(self, name: str) -> str | None:
        reference = self.environ.get(f"{name}_OP_REF")
        if not reference:
            return None
        if not _OP_REF_RE.match(reference):
            raise ValueError(
                f"{name}_OP_REF must look like op://vault/item/field"
            )
        return reference

    def resolve(self, name: str) -> str | None:
        reference = self.reference_for(name)
        if reference is None:
            return None
        executable = self.executable or shutil.which("op")
        if not executable:
            return None
        try:
            completed = self.runner(
                [executable, "read", "--no-newline", reference],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=_minimal_env("OP_"),
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        value = completed.stdout.rstrip("\r\n")
        return value if value else None

    def source_for(self, name: str) -> str | None:
        reference = self.reference_for(name)
        if reference is None or not self.resolve(name):
            return None
        return f"1Password ({reference})"


def key_rotated_at(
    resolver: Any,
    name: str,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> datetime | None:
    """Best-effort key age: ``<NAME>_ROTATED_AT``, else Keychain item date."""

    resolvers = getattr(resolver, "resolvers", (resolver,))
    if environ is None:
        environ = next(
            (r.environ for r in resolvers if hasattr(r, "environ")),
            os.environ,
        )
    stated = environ.get(f"{name}_ROTATED_AT")
    if stated:
        try:
            value = datetime.fromisoformat(stated)
        except ValueError:
            value = None
        if value is not None:
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    for item in resolvers:
        if hasattr(item, "updated_at") and item.resolve(name):
            updated = item.updated_at(name)
            if updated is not None:
                return updated
    return None


def default_secret_resolver(
    environ: dict[str, str] | os._Environ[str] = os.environ,
) -> SecretResolver:
    """Resolve keys in order: secret command, Keychain, 1Password, env.

    Keychain is used when ``MODEL_COUNCIL_KEYCHAIN_SERVICE`` is set (macOS).
    1Password is used for any key with a ``<NAME>_OP_REF`` reference.
    """

    resolvers: list[SecretResolver] = []
    command = environ.get("MODEL_COUNCIL_SECRET_COMMAND")
    if command:
        resolvers.append(CommandSecretResolver.from_string(command))
    keychain_service = environ.get("MODEL_COUNCIL_KEYCHAIN_SERVICE")
    if keychain_service and sys.platform == "darwin":
        resolvers.append(KeychainSecretResolver(service=keychain_service))
    if any(key.endswith("_OP_REF") for key in environ):
        resolvers.append(OnePasswordSecretResolver(environ))
    resolvers.append(EnvironmentSecretResolver(environ))
    return ChainedSecretResolver(tuple(resolvers))
