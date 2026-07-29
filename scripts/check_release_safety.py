#!/usr/bin/env python3
"""Fail closed when release-critical source defaults or paths drift."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml


DEFAULT_ROOT = Path(__file__).resolve().parent.parent
_SAFE_FINDING_PATH = re.compile(
    r"(?:[A-Za-z0-9._@+-]+/)*[A-Za-z0-9._@+-]+"
)


@dataclass(frozen=True, order=True, slots=True)
class ReleaseViolation:
    """One value-free, deterministic release-gate finding."""

    code: str
    path: str
    line: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", self.code) is None:
            raise ValueError("release violation code is invalid")
        if (
            not isinstance(self.path, str)
            or not self.path
            or self.path != "."
            and _SAFE_FINDING_PATH.fullmatch(self.path) is None
            or self.path.startswith(("/", "\\"))
            or "://" in self.path
            or any(
                char in self.path
                for char in ("\n", "\r", "\t", "\x1b", "?", "#", ":")
            )
            or "\\" in self.path
            or not self.path.isascii()
        ):
            raise ValueError("release violation path is invalid")
        parts = PurePosixPath(self.path).parts
        if self.path != "." and (
            not parts or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("release violation path is invalid")
        if (
            isinstance(self.line, bool)
            or not isinstance(self.line, int)
            or self.line <= 0
        ):
            raise ValueError("release violation line is invalid")


def _finding(code: str, relative: str, line: int) -> ReleaseViolation:
    safe_path = (
        relative
        if (
            isinstance(relative, str)
            and (
                relative == "."
                or _SAFE_FINDING_PATH.fullmatch(relative) is not None
            )
            and relative.isascii()
        )
        else "unsafe-path"
    )
    safe_line = line if type(line) is int and line > 0 else 1
    return ReleaseViolation(code, safe_path, safe_line)


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _git_tracked_names(root: Path) -> tuple[str, ...] | None:
    """Return tracked root-relative paths without opening any tracked file."""

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            check=False,
            capture_output=True,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--"],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0 or tracked.returncode != 0:
        return None
    try:
        git_root = Path(os.fsdecode(top.stdout).strip()).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if git_root != root:
        return None
    names: list[str] = []
    for raw_name in tracked.stdout.split(b"\0"):
        if not raw_name:
            continue
        try:
            name = raw_name.decode("utf-8")
        except UnicodeDecodeError:
            return None
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            return None
        names.append(name)
    return tuple(names)


def _security_sensitive_paths(root: Path) -> tuple[Path, ...]:
    fixed = (
        root / "config.yaml",
        root / ".env.example",
        root / "pyproject.toml",
        root / "uv.lock",
        root / "README.md",
        root / "docs" / "RUNBOOK.md",
        root / "docs" / "ops" / "README.md",
        root / "scripts" / "launchd" / "README.md",
    )
    discovered: list[Path] = []
    source_root = root / "src" / "trading_assistant"
    if source_root.exists() and not source_root.is_symlink():
        for current, directories, files in os.walk(
            source_root,
            followlinks=False,
        ):
            current_path = Path(current)
            discovered.extend(current_path / name for name in directories)
            discovered.extend(
                current_path / name
                for name in files
                if name.endswith(".py")
            )
    docs_root = root / "docs" / "superpowers"
    if docs_root.exists() and not docs_root.is_symlink():
        for current, directories, files in os.walk(
            docs_root,
            followlinks=False,
        ):
            current_path = Path(current)
            discovered.extend(current_path / name for name in directories)
            discovered.extend(
                current_path / name
                for name in files
                if name.endswith(".md")
            )
    return tuple((*fixed, *discovered))


def _scan_hermetic_root(root: Path) -> list[ReleaseViolation]:
    """Prove Git and every scanned security file are rooted exactly here."""

    names = _git_tracked_names(root)
    if names is None:
        return [_finding("GIT_TREE_UNPROVEN", ".", 1)]
    candidates = set(_security_sensitive_paths(root))
    candidates.update(root / PurePosixPath(name) for name in names)
    try:
        for candidate in sorted(candidates, key=lambda item: item.as_posix()):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            relative = candidate.relative_to(root)
            current = root
            for part in relative.parts:
                current = current / part
                if current.is_symlink():
                    return [_finding("GIT_TREE_UNPROVEN", ".", 1)]
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root):
                return [_finding("GIT_TREE_UNPROVEN", ".", 1)]
    except (OSError, RuntimeError, ValueError):
        return [_finding("GIT_TREE_UNPROVEN", ".", 1)]
    return []


_AUTHORITY_MUTATORS = frozenset(
    {
        "add",
        "append",
        "clear",
        "discard",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)


def _assignment_targets(node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, ast.Assign):
        return tuple(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return (node.target,)
    return ()


def _target_root_name(node: ast.AST | None) -> str | None:
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def _canonical_assignment_finding(
    tree: ast.Module,
    *,
    name: str,
    code: str,
    relative: str,
) -> list[ReleaseViolation]:
    all_definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in _assignment_targets(node)
        )
    ]
    direct_definitions = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in _assignment_targets(node)
        )
    ]
    if len(all_definitions) != 1 or len(direct_definitions) != 1:
        line = (
            getattr(all_definitions[1], "lineno", 1)
            if len(all_definitions) > 1
            else getattr(all_definitions[0], "lineno", 1)
            if all_definitions
            else 1
        )
        return [_finding(code, relative, line)]

    aliases = {name}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if _target_root_name(node.value) not in aliases:
                continue
            for target in _assignment_targets(node):
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True

    mutator_aliases: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            binds_mutator = (
                isinstance(value, ast.Attribute)
                and value.attr in _AUTHORITY_MUTATORS
                and _target_root_name(value.value) in aliases
            ) or (
                isinstance(value, ast.Name)
                and value.id in mutator_aliases
            )
            if not binds_mutator:
                continue
            for target in _assignment_targets(node):
                if (
                    isinstance(target, ast.Name)
                    and target.id not in mutator_aliases
                ):
                    mutator_aliases.add(target.id)
                    changed = True

    canonical = direct_definitions[0]
    for node in ast.walk(tree):
        if node is canonical:
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"globals", "vars"}
        ):
            return [_finding(code, relative, node.lineno)]
        if isinstance(node, ast.AugAssign):
            if _target_root_name(node.target) in aliases:
                return [_finding(code, relative, node.lineno)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in _assignment_targets(node):
                root_name = _target_root_name(target)
                if (
                    root_name in aliases
                    and not (
                        isinstance(target, ast.Name)
                        and isinstance(node.value, ast.Name)
                        and node.value.id in aliases
                    )
                ):
                    return [_finding(code, relative, node.lineno)]
        elif isinstance(node, ast.Delete):
            if any(
                _target_root_name(target) in aliases
                for target in node.targets
            ):
                return [_finding(code, relative, node.lineno)]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _AUTHORITY_MUTATORS
            and _target_root_name(node.func.value) in aliases
        ):
            return [_finding(code, relative, node.lineno)]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in mutator_aliases
        ):
            return [_finding(code, relative, node.lineno)]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and _target_root_name(node.args[0]) in aliases
        ):
            return [_finding(code, relative, node.lineno)]
    return []


def _scan_canonical_authorities(root: Path) -> list[ReleaseViolation]:
    findings: list[ReleaseViolation] = []
    assignments = (
        (
            "src/trading_assistant/app/agent.py",
            "READ_ONLY_TOOL_SPECS",
            "CHAT_TOOL_REGISTRY_UNPROVEN",
        ),
        (
            "src/trading_assistant/security/sensitive_fields.py",
            "SENSITIVE_FIELDS",
            "SENSITIVE_REGISTRY_INVALID",
        ),
        (
            "src/trading_assistant/security/secrets.py",
            "_SIMPLE_SECRET_FIELDS",
            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        ),
        (
            "src/trading_assistant/security/outbound.py",
            "OUTBOUND_ORIGIN_MANIFEST",
            "OUTBOUND_ORIGIN_UNAPPROVED",
        ),
    )
    for relative, name, code in assignments:
        path = root / relative
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            findings.append(_finding(code, relative, 1))
            continue
        findings.extend(
            _canonical_assignment_finding(
                tree,
                name=name,
                code=code,
                relative=relative,
            )
        )

    relative = "src/trading_assistant/config.py"
    path = root / relative
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return [
            *findings,
            _finding("COMPOSIO_ENABLED", relative, 1),
            _finding("WEBHOOK_ROUTE_PRESENT", relative, 1),
        ]
    classes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.name == "IntegrationsConfig"
    ]
    direct_classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "IntegrationsConfig"
    ]
    if len(classes) != 1 or len(direct_classes) != 1:
        line = getattr(classes[1] if len(classes) > 1 else classes[0], "lineno", 1) if classes else 1
        findings.extend(
            (
                _finding("COMPOSIO_ENABLED", relative, line),
                _finding("WEBHOOK_ROUTE_PRESENT", relative, line),
            )
        )
        return findings
    integration_class = direct_classes[0]
    aliases = {"IntegrationsConfig"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                for target in _assignment_targets(node):
                    if (
                        isinstance(target, ast.Name)
                        and target.id not in aliases
                    ):
                        aliases.add(target.id)
                        changed = True
    for field_name, code in (
        ("composio_enabled", "COMPOSIO_ENABLED"),
        ("webhooks_enabled", "WEBHOOK_ROUTE_PRESENT"),
    ):
        direct = [
            node
            for node in integration_class.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == field_name
        ]
        all_fields = [
            node
            for node in ast.walk(integration_class)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == field_name
                for target in _assignment_targets(node)
            )
        ]
        if len(direct) != 1 or len(all_fields) != 1:
            findings.append(
                _finding(
                    code,
                    relative,
                    getattr(all_fields[-1], "lineno", integration_class.lineno)
                    if all_fields
                    else integration_class.lineno,
                )
            )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for target in _assignment_targets(node):
                if (
                    isinstance(target, ast.Attribute)
                    and _target_root_name(target) in aliases
                    and target.attr in {
                        "composio_enabled",
                        "webhooks_enabled",
                    }
                ):
                    findings.append(
                        _finding(
                            "COMPOSIO_ENABLED"
                            if target.attr == "composio_enabled"
                            else "WEBHOOK_ROUTE_PRESENT",
                            relative,
                            node.lineno,
                        )
                    )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and _target_root_name(node.args[0]) in aliases
        ):
            field_name = _literal_string(node.args[1])
            if field_name == "composio_enabled":
                findings.append(
                    _finding("COMPOSIO_ENABLED", relative, node.lineno)
                )
            elif field_name == "webhooks_enabled":
                findings.append(
                    _finding("WEBHOOK_ROUTE_PRESENT", relative, node.lineno)
                )
            else:
                findings.extend(
                    (
                        _finding("COMPOSIO_ENABLED", relative, node.lineno),
                        _finding(
                            "WEBHOOK_ROUTE_PRESENT",
                            relative,
                            node.lineno,
                        ),
                    )
                )
    return findings


def _check_config(root: Path) -> None:
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    expected = {
        "trading.mode": config["trading"]["mode"] == "paper",
        "trading.broker": config["trading"]["broker"] == "alpaca",
        "features.auto_execute_preapproved_rules": (
            config["features"]["auto_execute_preapproved_rules"] is False
        ),
        "execution.prefer_bracket_orders": (
            config["execution"]["prefer_bracket_orders"] is False
        ),
        "llm.fallback_provider": config["llm"]["fallback_provider"] is None,
    }
    failed = [name for name, safe in expected.items() if not safe]
    if failed:
        _fail("unsafe config defaults: " + ", ".join(failed))


def _call_aliases(
    tree: ast.AST,
    attributes: set[str],
    *,
    dynamic_getattr: bool,
) -> set[str]:
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in attributes:
                    aliases.add(imported.asname or imported.name)
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            names = {
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            }
            if not names:
                continue
            is_alias = (
                isinstance(value, ast.Attribute)
                and value.attr in attributes
            ) or (
                isinstance(value, ast.Name)
                and value.id in aliases
            )
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "getattr"
                and len(value.args) >= 2
            ):
                attribute = value.args[1]
                is_alias = (
                    isinstance(attribute, ast.Constant)
                    and attribute.value in attributes
                ) or (
                    dynamic_getattr
                    and not isinstance(attribute, ast.Constant)
                )
            if is_alias and not names.issubset(aliases):
                aliases.update(names)
                changed = True
    return aliases


def _getattr_call(
    node: ast.AST,
    attributes: set[str],
    *,
    dynamic: bool,
) -> bool:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Name)
        or node.func.id != "getattr"
        or len(node.args) < 2
    ):
        return False
    attribute = node.args[1]
    return (
        isinstance(attribute, ast.Constant)
        and attribute.value in attributes
    ) or (dynamic and not isinstance(attribute, ast.Constant))


def _check_no_runtime_create_all(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    offenders: list[str] = []
    for path in runtime.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        aliases = _call_aliases(
            tree,
            {"create_all"},
            dynamic_getattr=False,
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_all"
            )
            aliased = (
                isinstance(node.func, ast.Name)
                and node.func.id in aliases
            )
            via_getattr = _getattr_call(
                node.func,
                {"create_all"},
                dynamic=False,
            )
            if direct or aliased or via_getattr:
                offenders.append(f"{relative}:{node.lineno}")
    if offenders:
        _fail("runtime create_all calls: " + ", ".join(offenders))


def _check_submission_paths(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    allowed = {
        "src/trading_assistant/orders/submission.py",
        "src/trading_assistant/broker/alpaca.py",
        "src/trading_assistant/broker/mock.py",
        "src/trading_assistant/backtest/engine.py",
        "src/trading_assistant/backtest/sim_broker.py",
        # A BrokerClient decorator. Its delegate is invoked only by the real
        # OrderSubmissionService so the drill can simulate response loss.
        "src/trading_assistant/ops/safety_drill.py",
    }
    offenders: list[str] = []
    submit_methods = {"submit_order", "submit_bracket"}
    for path in runtime.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        tenure_guarded_delegates: set[int] = set()
        if relative == "src/trading_assistant/ops/tenure.py":
            for class_node in tree.body:
                if not (
                    isinstance(class_node, ast.ClassDef)
                    and class_node.name == "TenureGuardedBroker"
                ):
                    continue
                for function in class_node.body:
                    if not (
                        isinstance(function, ast.FunctionDef)
                        and function.name in submit_methods
                    ):
                        continue
                    guarded = any(
                        isinstance(candidate, ast.Call)
                        and isinstance(candidate.func, ast.Attribute)
                        and candidate.func.attr == "ensure_owned"
                        and isinstance(candidate.func.value, ast.Attribute)
                        and candidate.func.value.attr == "__guard"
                        and isinstance(candidate.func.value.value, ast.Name)
                        and candidate.func.value.value.id == "self"
                        for candidate in ast.walk(function)
                    )
                    if not guarded:
                        continue
                    for candidate in ast.walk(function):
                        if not (
                            isinstance(candidate, ast.Call)
                            and isinstance(candidate.func, ast.Attribute)
                            and candidate.func.attr == function.name
                            and isinstance(
                                candidate.func.value,
                                ast.Attribute,
                            )
                            and candidate.func.value.attr == "__broker"
                            and isinstance(
                                candidate.func.value.value,
                                ast.Name,
                            )
                            and candidate.func.value.value.id == "self"
                        ):
                            continue
                        tenure_guarded_delegates.add(candidate.lineno)
        aliases = _call_aliases(
            tree,
            submit_methods,
            dynamic_getattr=True,
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in submit_methods
            )
            aliased = (
                isinstance(node.func, ast.Name)
                and node.func.id in aliases
            )
            via_getattr = _getattr_call(
                node.func,
                submit_methods,
                dynamic=True,
            )
            if not (direct or aliased or via_getattr):
                continue
            allowed_call = relative in allowed
            if (
                relative == "src/trading_assistant/ops/tenure.py"
                and node.lineno in tenure_guarded_delegates
            ):
                allowed_call = True
            if relative == "src/trading_assistant/ops/safety_drill.py":
                delegate = (
                    node.func.value
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                allowed_call = (
                    direct
                    and node.func.attr == "submit_order"
                    and isinstance(delegate, ast.Attribute)
                    and delegate.attr == "_broker"
                    and isinstance(delegate.value, ast.Name)
                    and delegate.value.id == "self"
                )
            if aliased or via_getattr:
                allowed_call = False
            if not allowed_call:
                offenders.append(f"{relative}:{node.lineno}")
    if offenders:
        _fail("broker submission outside approved paths: " + ", ".join(offenders))


def _check_no_raw_broker_escape(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    allowed = {
        "src/trading_assistant/broker/alpaca.py",
        "src/trading_assistant/ops/tenure.py",
        "src/trading_assistant/ops/safety_drill.py",
    }
    private_names = {
        "_broker",
        "__broker",
        "_TenureGuardedBroker__broker",
        "_trading",
        "_data",
        "_crypto_data",
    }
    offenders: list[str] = []
    for path in runtime.rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative in allowed:
            continue
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=relative,
        )
        for node in ast.walk(tree):
            direct = (
                isinstance(node, ast.Attribute)
                and node.attr in private_names
            )
            dynamic = (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in private_names
            )
            if direct or dynamic:
                offenders.append(f"{relative}:{node.lineno}")
    if offenders:
        _fail("raw broker escape: " + ", ".join(sorted(set(offenders))))


def _check_browser_sources(root: Path) -> None:
    static = root / "src" / "trading_assistant" / "app" / "static"
    forbidden = {
        "localStorage": re.compile(r"\blocalStorage\b"),
        "X-API-Key": re.compile(r"\bX-API-Key\b", re.IGNORECASE),
    }
    offenders: list[str] = []
    for path in static.rglob("*"):
        if path.suffix not in {".html", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for name, pattern in forbidden.items():
            if pattern.search(text):
                offenders.append(f"{path.relative_to(root).as_posix()}:{name}")
        inline_pattern = re.compile(
            r"(?:<[^>]*\son[a-z][a-z0-9_:-]*\s*=|"
            r"\.\s*on[a-z][a-z0-9_$]*\s*=)",
            re.IGNORECASE | re.DOTALL,
        )
        if inline_pattern.search(text):
            offenders.append(
                f"{path.relative_to(root).as_posix()}:inline event handler"
            )
    if offenders:
        _fail("unsafe browser source: " + ", ".join(offenders))


def _check_no_unofficial_robinhood_dependency(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    paths = [
        root / "pyproject.toml",
        root / "uv.lock",
        *runtime.rglob("*.py"),
    ]
    pattern = re.compile(r"robin[_-]?stocks|robinhood[_-]?api", re.IGNORECASE)
    offenders = [
        path.relative_to(root).as_posix()
        for path in paths
        if pattern.search(path.read_text(encoding="utf-8"))
    ]
    if offenders:
        _fail("unofficial Robinhood dependency/import: " + ", ".join(offenders))


_ROUTE_DECORATOR_METHODS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "trace": "TRACE",
}
_GENERIC_ROUTE_DECORATORS = {"api_route", "route"}
_WEBSOCKET_ROUTE_DECORATORS = {"websocket", "websocket_route"}
_IMPERATIVE_HTTP_REGISTRATIONS = {"add_api_route", "add_route"}
_IMPERATIVE_WEBSOCKET_REGISTRATIONS = {"add_websocket_route"}
_ROUTE_MOUNT_REGISTRATIONS = {"mount"}
_NON_ROUTE_DECORATORS = {"field_validator", "model_validator", "wraps"}
_LLM_BACKEND_CLASSES = {
    "AnthropicBackend",
    "GeminiBackend",
    "GroqBackend",
}
_LLM_BACKEND_MODULES = {
    "anthropic_backend",
    "gemini_backend",
    "groq_backend",
}
_LLM_BACKEND_MODULE_PATHS = {
    f"trading_assistant.llm.{module}"
    for module in _LLM_BACKEND_MODULES
}
_LLM_BACKEND_ALLOWED_PATHS = {
    "src/trading_assistant/llm/factory.py",
    "src/trading_assistant/llm/anthropic_backend.py",
    "src/trading_assistant/llm/gemini_backend.py",
    "src/trading_assistant/llm/groq_backend.py",
}
_LLM_FACTORY_PATH = "src/trading_assistant/llm/factory.py"
_LLM_WRAPPER_PATH = "src/trading_assistant/llm/base.py"
_RAW_LLM_FACTORY_HELPERS = {"_make_backend"}
_DIRECT_LLM_DELEGATE_ATTRIBUTES = {
    "delegate",
    "_delegate",
    "__delegate",
    "_BudgetedLLMBackend__delegate",
}


def _literal_route_policies(root: Path) -> set[tuple[str, str]]:
    relative = "src/trading_assistant/app/policy.py"
    tree = ast.parse(
        (root / relative).read_text(encoding="utf-8"),
        filename=relative,
    )
    registry = next(
        (
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "ROUTE_POLICIES"
                for target in node.targets
            )
        ),
        None,
    )
    if not isinstance(registry, (ast.List, ast.Tuple)):
        _fail("ROUTE_POLICIES must be a literal route registry")

    policies: set[tuple[str, str]] = set()
    for entry in registry.elts:
        if (
            not isinstance(entry, ast.Call)
            or not isinstance(entry.func, ast.Name)
            or entry.func.id != "RoutePolicy"
            or len(entry.args) < 2
            or not isinstance(entry.args[0], ast.Constant)
            or not isinstance(entry.args[0].value, str)
            or not isinstance(entry.args[1], ast.Constant)
            or not isinstance(entry.args[1].value, str)
        ):
            _fail("ROUTE_POLICIES must use literal RoutePolicy method/path")
        policies.add((entry.args[0].value.upper(), entry.args[1].value))
    return policies


def _route_decorator_aliases(
    tree: ast.AST,
    relative: str,
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    assignments = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
        ),
        key=lambda node: (node.lineno, node.col_offset),
    )
    route_decorators = (
        set(_ROUTE_DECORATOR_METHODS)
        | _GENERIC_ROUTE_DECORATORS
        | _WEBSOCKET_ROUTE_DECORATORS
    )
    for node in assignments:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        names = {
            target.id
            for target in targets
            if isinstance(target, ast.Name)
        }
        if not names:
            continue
        value = node.value
        decorator = (
            value.attr
            if isinstance(value, ast.Attribute)
            else aliases.get(value.id)
            if isinstance(value, ast.Name)
            else None
        )
        if decorator not in route_decorators:
            continue
        for name in names:
            previous = aliases.get(name)
            if previous is not None and previous != decorator:
                _fail(
                    f"conflicting route decorator alias: "
                    f"{relative}:{node.lineno}"
                )
            aliases[name] = decorator
    return aliases


def _decorated_routes(path: Path, root: Path) -> list[tuple[str, str]]:
    relative = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    aliases = _route_decorator_aliases(tree, relative)
    routes: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if isinstance(decorator.func, ast.Attribute):
                decorator_name = decorator.func.attr
            elif isinstance(decorator.func, ast.Name):
                decorator_name = aliases.get(decorator.func.id)
                if decorator_name is None:
                    if decorator.func.id in _NON_ROUTE_DECORATORS:
                        continue
                    _fail(
                        f"unresolved route decorator: "
                        f"{relative}:{node.lineno}"
                    )
            else:
                _fail(
                    f"unresolved route decorator: {relative}:{node.lineno}"
                )
            if decorator_name not in (
                set(_ROUTE_DECORATOR_METHODS)
                | _GENERIC_ROUTE_DECORATORS
                | _WEBSOCKET_ROUTE_DECORATORS
                | _NON_ROUTE_DECORATORS
            ):
                _fail(
                    f"unresolved route decorator: {relative}:{node.lineno}"
                )
            if decorator_name in _NON_ROUTE_DECORATORS:
                continue
            if decorator_name in _WEBSOCKET_ROUTE_DECORATORS:
                _fail(
                    f"websocket route registration: "
                    f"{relative}:{node.lineno}"
                )
            method = _ROUTE_DECORATOR_METHODS.get(decorator_name)
            if method is None and decorator_name not in _GENERIC_ROUTE_DECORATORS:
                continue
            path_arg = decorator.args[0] if decorator.args else None
            if (
                not isinstance(path_arg, ast.Constant)
                or not isinstance(path_arg.value, str)
            ):
                _fail(
                    f"route decorator must use literal path: "
                    f"{relative}:{node.lineno}"
                )
            route_path = path_arg.value
            if method is not None:
                routes.append((method, route_path))
                continue
            methods_arg = next(
                (
                    keyword.value
                    for keyword in decorator.keywords
                    if keyword.arg == "methods"
                ),
                None,
            )
            if not isinstance(methods_arg, (ast.List, ast.Set, ast.Tuple)):
                _fail(
                    f"api_route must use literal methods: "
                    f"{relative}:{node.lineno}"
                )
            for method_arg in methods_arg.elts:
                if (
                    not isinstance(method_arg, ast.Constant)
                    or not isinstance(method_arg.value, str)
                ):
                    _fail(
                        f"api_route must use literal methods: "
                        f"{relative}:{node.lineno}"
                    )
                routes.append((method_arg.value.upper(), route_path))
    return routes


def _is_registered_call(
    node: ast.Call,
    methods: set[str],
    aliases: set[str],
) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in methods
    ) or (
        isinstance(node.func, ast.Name)
        and node.func.id in aliases
    ) or _getattr_call(
        node.func,
        methods,
        dynamic=True,
    )


def _check_imperative_route_registrations(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    for path in sorted(runtime.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        http_aliases = _call_aliases(
            tree,
            _IMPERATIVE_HTTP_REGISTRATIONS,
            dynamic_getattr=True,
        )
        websocket_aliases = _call_aliases(
            tree,
            _IMPERATIVE_WEBSOCKET_REGISTRATIONS,
            dynamic_getattr=True,
        )
        mount_aliases = _call_aliases(
            tree,
            _ROUTE_MOUNT_REGISTRATIONS,
            dynamic_getattr=True,
        )
        for node in sorted(
            (
                candidate
                for candidate in ast.walk(tree)
                if isinstance(candidate, ast.Call)
            ),
            key=lambda candidate: (
                candidate.lineno,
                candidate.col_offset,
            ),
        ):
            if _is_registered_call(
                node,
                _IMPERATIVE_HTTP_REGISTRATIONS,
                http_aliases,
            ):
                _fail(
                    f"imperative HTTP route registration: "
                    f"{relative}:{node.lineno}"
                )
            if _is_registered_call(
                node,
                _IMPERATIVE_WEBSOCKET_REGISTRATIONS,
                websocket_aliases,
            ):
                _fail(
                    f"websocket route registration: "
                    f"{relative}:{node.lineno}"
                )
            if not _is_registered_call(
                node,
                _ROUTE_MOUNT_REGISTRATIONS,
                mount_aliases,
            ):
                continue
            mount_path = node.args[0] if node.args else None
            if (
                isinstance(mount_path, ast.Constant)
                and mount_path.value == "/static"
            ):
                continue
            _fail(
                f"non-allowlisted route mount: "
                f"{relative}:{node.lineno}"
            )


def _check_route_policy_inventory(root: Path) -> None:
    app = root / "src" / "trading_assistant" / "app"
    route_files = [app / "main.py", *sorted((app / "routers").glob("*.py"))]
    policies = _literal_route_policies(root)
    decorated_routes = {
        route
        for path in route_files
        if path.exists()
        for route in _decorated_routes(path, root)
    }
    _check_imperative_route_registrations(root)
    missing = sorted(
        {
            route
            for route in decorated_routes
            if route not in policies
        }
    )
    if missing:
        method, path = missing[0]
        _fail(f"route missing from ROUTE_POLICIES: {method} {path}")


@dataclass(frozen=True, slots=True)
class _RouteConstructor:
    kind: str


@dataclass(frozen=True, slots=True)
class _StaticAssetConstructor:
    pass


@dataclass(frozen=True, slots=True)
class _StaticAssetValue:
    pass


@dataclass(frozen=True, slots=True)
class _RouteModule:
    name: str


@dataclass(frozen=True, slots=True)
class _RouteImport:
    module: str
    symbol: str
    line: int


@dataclass(frozen=True, slots=True)
class _RouteFactory:
    module: str
    function: ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True, slots=True)
class _RouteMethod:
    receiver: "_RouteObject"
    name: str
    line: int


@dataclass(frozen=True, slots=True)
class _DynamicRouteMethod:
    receiver: "_RouteObject"
    line: int


@dataclass(frozen=True, slots=True)
class _UnknownRouteValue:
    line: int


@dataclass(frozen=True, slots=True)
class _OpaqueRouteValue:
    line: int


@dataclass(frozen=True, slots=True)
class _StaticRoute:
    method: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class _StaticRouteInclude:
    child: "_RouteObject | None"
    prefix: str | None
    line: int
    mount: bool = False
    static_assets: bool = False


@dataclass(slots=True)
class _RouteObject:
    key: str
    kind: str
    prefix: str | None
    line: int
    routes: list[_StaticRoute] = field(default_factory=list)
    includes: list[_StaticRouteInclude] = field(default_factory=list)


def _route_module_name(path: Path, source_root: Path) -> str:
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _route_import_module(
    current_module: str,
    node: ast.ImportFrom,
) -> str | None:
    if node.level == 0:
        return node.module
    package = current_module.split(".")[:-1]
    parents = node.level - 1
    if parents > len(package):
        return None
    if parents:
        package = package[:-parents]
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _route_join(prefix: str, path: str) -> str:
    if prefix == "" and path == "":
        return ""
    if not prefix:
        joined = path or "/"
    elif path == "":
        joined = prefix
    else:
        joined = f"{prefix.rstrip('/')}/{path.lstrip('/')}"
    if not joined.startswith("/"):
        joined = f"/{joined}"
    return joined


def _route_literal_methods(node: ast.AST | None) -> tuple[str, ...] | None:
    if node is None:
        return ("GET",)
    if not isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return None
    methods: list[str] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(
            element.value, str
        ):
            return None
        methods.append(element.value.upper())
    return tuple(methods) if methods else None


def _effective_route_shape(path: str) -> str:
    normalized = re.sub(r"/+", "/", path)
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return re.sub(
        r"\{[^{}:]+(?::([^{}]+))?\}",
        lambda match: f"{{{match.group(1) or 'str'}}}",
        normalized,
    )


class _EffectiveRouteGraph:
    """Conservatively resolve the FastAPI graph rooted at app.main."""

    _SAFE_APP_METHODS = frozenset(
        {
            "add_event_handler",
            "add_exception_handler",
            "add_middleware",
            "exception_handler",
            "middleware",
        }
    )
    _DYNAMIC_IMPORT_NAMES = frozenset({"__import__", "import_module"})

    def __init__(self, root: Path) -> None:
        self.root = root
        self.source_root = root / "src"
        app_root = self.source_root / "trading_assistant" / "app"
        self.modules: dict[str, tuple[Path, ast.Module]] = {}
        self.module_envs: dict[str, dict[str, Any]] = {}
        self._evaluating_modules: set[str] = set()
        self._function_results: dict[
            tuple[Any, ...], _RouteObject | _UnknownRouteValue
        ] = {}
        self._active_functions: set[tuple[str, int]] = set()
        self._object_sequence = 0
        self._objects: list[_RouteObject] = []
        self.findings: list[ReleaseViolation] = []
        if app_root.exists():
            for path in sorted(app_root.rglob("*.py")):
                relative = path.relative_to(root).as_posix()
                try:
                    tree = ast.parse(
                        path.read_text(encoding="utf-8"),
                        filename=relative,
                    )
                except (OSError, SyntaxError):
                    self.findings.append(
                        _finding("ROUTE_REGISTRATION_UNPROVEN", relative, 1)
                    )
                    continue
                self.modules[_route_module_name(path, self.source_root)] = (
                    path,
                    tree,
                )

    def _relative(self, module: str) -> str:
        item = self.modules.get(module)
        if item is None:
            return "src/trading_assistant/app/main.py"
        return item[0].relative_to(self.root).as_posix()

    def _unproven(self, module: str, line: int) -> _UnknownRouteValue:
        self.findings.append(
            _finding(
                "ROUTE_REGISTRATION_UNPROVEN",
                self._relative(module),
                max(1, line),
            )
        )
        return _UnknownRouteValue(max(1, line))

    def _new_object(
        self,
        module: str,
        kind: str,
        prefix: str | None,
        line: int,
    ) -> _RouteObject:
        self._object_sequence += 1
        created = _RouteObject(
            key=f"{module}:{line}:{self._object_sequence}",
            kind=kind,
            prefix=prefix,
            line=line,
        )
        self._objects.append(created)
        return created

    def _resolve(self, value: Any, seen: set[tuple[str, str]] | None = None) -> Any:
        if not isinstance(value, _RouteImport):
            return value
        if value.module == "fastapi" and value.symbol in {
            "FastAPI",
            "APIRouter",
        }:
            return _RouteConstructor(value.symbol)
        if (
            value.module in {"fastapi.staticfiles", "starlette.staticfiles"}
            and value.symbol == "StaticFiles"
        ):
            return _StaticAssetConstructor()
        if value.module not in self.modules:
            return _OpaqueRouteValue(value.line)
        marker = (value.module, value.symbol)
        active = set() if seen is None else set(seen)
        if marker in active:
            return self._unproven(value.module, value.line)
        active.add(marker)
        env = self._evaluate_module(value.module)
        resolved = env.get(value.symbol)
        if resolved is None:
            return _OpaqueRouteValue(value.line)
        return self._resolve(resolved, active)

    def _bind_import(
        self,
        env: dict[str, Any],
        module: str,
        statement: ast.Import | ast.ImportFrom,
    ) -> None:
        if isinstance(statement, ast.Import):
            for imported in statement.names:
                local = imported.asname or imported.name.split(".", 1)[0]
                env[local] = _RouteModule(imported.name)
            return
        target = _route_import_module(module, statement)
        for imported in statement.names:
            local = imported.asname or imported.name
            if imported.name == "*":
                self._unproven(module, statement.lineno)
                continue
            if target == "fastapi" and imported.name in {
                "FastAPI",
                "APIRouter",
            }:
                env[local] = _RouteConstructor(imported.name)
            elif target is not None:
                env[local] = _RouteImport(
                    target,
                    imported.name,
                    statement.lineno,
                )
            else:
                env[local] = _UnknownRouteValue(statement.lineno)

    def _bind_target(
        self,
        target: ast.AST,
        value: Any,
        env: dict[str, Any],
    ) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                if isinstance(element, ast.Name):
                    env[element.id] = _UnknownRouteValue(
                        getattr(target, "lineno", 1)
                    )

    def _evaluate_module(self, module: str) -> dict[str, Any]:
        if module in self.module_envs:
            return self.module_envs[module]
        item = self.modules.get(module)
        if item is None:
            return {}
        if module in self._evaluating_modules:
            return {}
        self._evaluating_modules.add(module)
        env: dict[str, Any] = {}
        self.module_envs[module] = env
        self._process_statements(item[1].body, module, env, [])
        self._evaluating_modules.remove(module)
        return env

    def _factory_call(
        self,
        factory: _RouteFactory,
        call: ast.Call,
        caller_env: dict[str, Any],
    ) -> _RouteObject | _UnknownRouteValue:
        parameters = [
            *factory.function.args.posonlyargs,
            *factory.function.args.args,
            *factory.function.args.kwonlyargs,
        ]
        supplied: dict[str, Any] = {}
        positional = [
            self._eval_expr(argument, factory.module, caller_env)
            for argument in call.args
            if not isinstance(argument, ast.Starred)
        ]
        for parameter, value in zip(parameters, positional):
            supplied[parameter.arg] = value
        for keyword in call.keywords:
            if keyword.arg is not None:
                supplied[keyword.arg] = self._eval_expr(
                    keyword.value,
                    factory.module,
                    caller_env,
                )
        def value_token(value: Any) -> tuple[Any, ...]:
            resolved = self._resolve(value)
            if isinstance(resolved, _RouteObject):
                return ("route", resolved.key)
            if isinstance(resolved, _UnknownRouteValue):
                return ("unknown", resolved.line)
            if isinstance(resolved, _OpaqueRouteValue):
                return ("opaque", resolved.line)
            if isinstance(resolved, _RouteModule):
                return ("module", resolved.name)
            if isinstance(resolved, _RouteConstructor):
                return ("constructor", resolved.kind)
            if isinstance(resolved, (str, int, bool, type(None))):
                return ("literal", type(resolved).__name__, resolved)
            return ("other", type(resolved).__name__)

        signature = tuple(
            (parameter.arg, value_token(supplied.get(
                parameter.arg,
                _UnknownRouteValue(factory.function.lineno),
            )))
            for parameter in parameters
        )
        mutates_receiver = any(
            isinstance(self._resolve(value), _RouteObject)
            for value in supplied.values()
        )
        cache_key = (
            factory.module,
            factory.function.lineno,
            signature,
            call.lineno if mutates_receiver else 0,
        )
        cached = self._function_results.get(cache_key)
        if cached is not None:
            return cached
        active_key = (factory.module, factory.function.lineno)
        if active_key in self._active_functions:
            return self._unproven(factory.module, factory.function.lineno)
        self._active_functions.add(active_key)
        env = dict(self._evaluate_module(factory.module))
        for parameter in parameters:
            env[parameter.arg] = supplied.get(
                parameter.arg,
                _UnknownRouteValue(factory.function.lineno),
            )
        returns: list[Any] = []
        self._process_statements(
            factory.function.body,
            factory.module,
            env,
            returns,
        )
        objects = [
            self._resolve(value)
            for value in returns
            if isinstance(self._resolve(value), _RouteObject)
        ]
        result: _RouteObject | _UnknownRouteValue
        if not objects:
            result = _UnknownRouteValue(factory.function.lineno)
        elif any(value is not objects[0] for value in objects[1:]):
            result = self._unproven(factory.module, factory.function.lineno)
        else:
            result = objects[0]
        self._function_results[cache_key] = result
        self._active_functions.remove(active_key)
        return result

    def _registration_path(
        self,
        receiver: _RouteObject,
        call: ast.Call,
        module: str,
        env: dict[str, Any],
    ) -> str | None:
        path_node = call.args[0] if call.args else next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg in {"path", "prefix"}
            ),
            None,
        )
        route_value = self._eval_expr(path_node, module, env)
        route_path = route_value if isinstance(route_value, str) else None
        if route_path is None:
            self._unproven(module, call.lineno)
            return None
        return route_path

    def _register_method(
        self,
        method: _RouteMethod,
        call: ast.Call,
        module: str,
        env: dict[str, Any],
    ) -> Any:
        receiver = method.receiver
        name = method.name
        if name in _ROUTE_DECORATOR_METHODS:
            route_path = self._registration_path(
                receiver,
                call,
                module,
                env,
            )
            if route_path is not None:
                receiver.routes.append(
                    _StaticRoute(
                        _ROUTE_DECORATOR_METHODS[name],
                        route_path,
                        call.lineno,
                    )
                )
            return _UnknownRouteValue(call.lineno)
        if name in _GENERIC_ROUTE_DECORATORS:
            route_path = self._registration_path(
                receiver,
                call,
                module,
                env,
            )
            methods_node = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "methods"
                ),
                None,
            )
            methods = _route_literal_methods(methods_node)
            if route_path is None or methods is None:
                self._unproven(module, call.lineno)
            else:
                receiver.routes.extend(
                    _StaticRoute(route_method, route_path, call.lineno)
                    for route_method in methods
                )
            return _UnknownRouteValue(call.lineno)
        if name in _WEBSOCKET_ROUTE_DECORATORS:
            self._unproven(module, call.lineno)
            return _UnknownRouteValue(call.lineno)
        if name in _IMPERATIVE_HTTP_REGISTRATIONS:
            route_path = self._registration_path(
                receiver,
                call,
                module,
                env,
            )
            methods_node = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "methods"
                ),
                None,
            )
            methods = _route_literal_methods(methods_node)
            if route_path is None or methods is None:
                self._unproven(module, call.lineno)
            else:
                receiver.routes.extend(
                    _StaticRoute(route_method, route_path, call.lineno)
                    for route_method in methods
                )
            return _UnknownRouteValue(call.lineno)
        if name in _IMPERATIVE_WEBSOCKET_REGISTRATIONS:
            self._unproven(module, call.lineno)
            return _UnknownRouteValue(call.lineno)
        if name == "include_router":
            child_node = call.args[0] if call.args else next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "router"
                ),
                None,
            )
            child = self._resolve(
                self._eval_expr(child_node, module, env)
                if child_node is not None
                else _UnknownRouteValue(call.lineno)
            )
            prefix_node = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "prefix"
                ),
                None,
            )
            prefix_value = (
                ""
                if prefix_node is None
                else self._eval_expr(prefix_node, module, env)
            )
            prefix = (
                prefix_value
                if isinstance(prefix_value, str)
                else None
            )
            if not isinstance(child, _RouteObject) or prefix is None:
                self._unproven(module, call.lineno)
                child = child if isinstance(child, _RouteObject) else None
            receiver.includes.append(
                _StaticRouteInclude(child, prefix, call.lineno)
            )
            return _UnknownRouteValue(call.lineno)
        if name == "mount":
            mount_path = self._registration_path(
                receiver,
                call,
                module,
                env,
            )
            child_node = (
                call.args[1]
                if len(call.args) >= 2
                else next(
                    (
                        keyword.value
                        for keyword in call.keywords
                        if keyword.arg == "app"
                    ),
                    None,
                )
            )
            mounted = self._resolve(
                self._eval_expr(child_node, module, env)
                if child_node is not None
                else _UnknownRouteValue(call.lineno)
            )
            child = mounted if isinstance(mounted, _RouteObject) else None
            static_assets = isinstance(mounted, _StaticAssetValue)
            receiver.includes.append(
                _StaticRouteInclude(
                    child,
                    mount_path,
                    call.lineno,
                    mount=True,
                    static_assets=static_assets,
                )
            )
            return _UnknownRouteValue(call.lineno)
        if name in self._SAFE_APP_METHODS:
            return _UnknownRouteValue(call.lineno)
        return _UnknownRouteValue(call.lineno)

    def _eval_expr(
        self,
        node: ast.AST | None,
        module: str,
        env: dict[str, Any],
    ) -> Any:
        if node is None:
            return _UnknownRouteValue(1)
        if isinstance(node, ast.Constant) and isinstance(
            node.value,
            (str, int, bool, type(None)),
        ):
            return node.value
        if isinstance(node, ast.Name):
            return self._resolve(
                env.get(node.id, _UnknownRouteValue(node.lineno))
            )
        if isinstance(node, ast.Attribute):
            base = self._resolve(self._eval_expr(node.value, module, env))
            if isinstance(base, _RouteObject):
                if node.attr == "router":
                    return base
                return _RouteMethod(base, node.attr, node.lineno)
            if (
                isinstance(base, _RouteMethod)
                and base.name == "routes"
                and node.attr in {"append", "extend", "insert"}
            ):
                return _DynamicRouteMethod(base.receiver, node.lineno)
            if isinstance(base, _RouteModule):
                return self._resolve(
                    _RouteImport(base.name, node.attr, node.lineno)
                )
            return _UnknownRouteValue(node.lineno)
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__getattribute__"
                and len(node.args) == 1
            ):
                receiver = self._resolve(
                    self._eval_expr(node.func.value, module, env)
                )
                attribute = _literal_string(node.args[0])
                if isinstance(receiver, _RouteObject):
                    if attribute is None:
                        self._unproven(module, node.lineno)
                        return _DynamicRouteMethod(receiver, node.lineno)
                    return _RouteMethod(receiver, attribute, node.lineno)
                return self._unproven(module, node.lineno)
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
            ):
                receiver = self._resolve(
                    self._eval_expr(node.args[0], module, env)
                )
                attribute = _literal_string(node.args[1])
                if isinstance(receiver, _RouteObject):
                    if attribute is None:
                        self._unproven(module, node.lineno)
                        return _DynamicRouteMethod(receiver, node.lineno)
                    return _RouteMethod(receiver, attribute, node.lineno)
                if isinstance(receiver, _RouteModule):
                    if attribute is None:
                        return self._unproven(module, node.lineno)
                    return self._resolve(
                        _RouteImport(
                            receiver.name,
                            attribute,
                            node.lineno,
                        )
                    )
                return _UnknownRouteValue(node.lineno)
            dynamic_import = (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
            ) or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            if dynamic_import:
                imported = _literal_string(node.args[0] if node.args else None)
                if imported is None or imported not in self.modules:
                    return self._unproven(module, node.lineno)
                return _RouteModule(imported)
            function = self._resolve(self._eval_expr(node.func, module, env))
            if isinstance(function, _RouteConstructor):
                prefix_node = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "prefix"
                    ),
                    None,
                )
                prefix_value = (
                    ""
                    if prefix_node is None
                    else self._eval_expr(prefix_node, module, env)
                )
                prefix = (
                    prefix_value
                    if isinstance(prefix_value, str)
                    else None
                )
                if prefix is None:
                    self._unproven(module, node.lineno)
                return self._new_object(
                    module,
                    function.kind,
                    prefix,
                    node.lineno,
                )
            if isinstance(function, _StaticAssetConstructor):
                return _StaticAssetValue()
            if isinstance(function, _RouteFactory):
                return self._factory_call(function, node, env)
            if isinstance(function, _RouteMethod):
                return self._register_method(function, node, module, env)
            if isinstance(function, _DynamicRouteMethod):
                return self._unproven(module, node.lineno)
            argument_values = [
                self._resolve(self._eval_expr(argument, module, env))
                for argument in node.args
            ]
            keyword_values = [
                self._resolve(self._eval_expr(keyword.value, module, env))
                for keyword in node.keywords
            ]
            if any(
                isinstance(value, _RouteObject)
                for value in (*argument_values, *keyword_values)
            ):
                self._unproven(module, node.lineno)
            return _UnknownRouteValue(node.lineno)
        if isinstance(node, ast.IfExp):
            left = self._eval_expr(node.body, module, env)
            right = self._eval_expr(node.orelse, module, env)
            return left if left is right else _UnknownRouteValue(node.lineno)
        return _UnknownRouteValue(getattr(node, "lineno", 1))

    def _process_statements(
        self,
        statements: list[ast.stmt],
        module: str,
        env: dict[str, Any],
        returns: list[Any],
    ) -> None:
        for statement in statements:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self._bind_import(env, module, statement)
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = self._eval_expr(statement.value, module, env)
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if isinstance(target, ast.Subscript):
                        resolved_container = self._resolve(
                            self._eval_expr(
                                target.value,
                                module,
                                env,
                            )
                        )
                        if (
                            isinstance(resolved_container, _RouteMethod)
                            and resolved_container.name == "routes"
                        ) or isinstance(
                            resolved_container,
                            _RouteObject,
                        ):
                            self._unproven(module, statement.lineno)
                    if isinstance(target, ast.Attribute):
                        resolved_target = self._resolve(
                            self._eval_expr(target, module, env)
                        )
                        if (
                            isinstance(resolved_target, _RouteMethod)
                            and resolved_target.name == "routes"
                        ):
                            self._unproven(module, statement.lineno)
                    if isinstance(target, ast.Name):
                        previous = self._resolve(env.get(target.id))
                        if (
                            isinstance(previous, _RouteMethod)
                            and isinstance(value, _RouteMethod)
                            and (
                                previous.receiver is not value.receiver
                                or previous.name != value.name
                            )
                        ):
                            self._unproven(module, statement.lineno)
                    self._bind_target(target, value, env)
            elif isinstance(statement, ast.AugAssign):
                target = statement.target
                resolved_target = self._resolve(
                    self._eval_expr(
                        target.value
                        if isinstance(target, (ast.Attribute, ast.Subscript))
                        else target,
                        module,
                        env,
                    )
                )
                if isinstance(resolved_target, (_RouteObject, _RouteMethod)):
                    self._unproven(module, statement.lineno)
                elif isinstance(target, (ast.Attribute, ast.Subscript)):
                    root = target
                    while isinstance(root, (ast.Attribute, ast.Subscript)):
                        root = root.value
                    if isinstance(
                        self._resolve(self._eval_expr(root, module, env)),
                        _RouteObject,
                    ):
                        self._unproven(module, statement.lineno)
            elif isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                for decorator in statement.decorator_list:
                    target = (
                        self._resolve(
                            self._eval_expr(
                                decorator.func,
                                module,
                                env,
                            )
                        )
                        if isinstance(decorator, ast.Call)
                        else None
                    )
                    self._eval_expr(decorator, module, env)
                    if (
                        not isinstance(
                            target,
                            (_RouteMethod, _DynamicRouteMethod),
                        )
                        and isinstance(decorator, ast.Call)
                        and decorator.args
                        and isinstance(decorator.args[0], ast.Constant)
                        and isinstance(decorator.args[0].value, str)
                        and decorator.args[0].value.startswith("/")
                    ):
                        self._unproven(module, decorator.lineno)
                env[statement.name] = _RouteFactory(module, statement)
            elif isinstance(statement, ast.ClassDef):
                bases = [
                    self._resolve(
                        self._eval_expr(base, module, env)
                    )
                    for base in statement.bases
                ]
                if any(
                    isinstance(base, _StaticAssetConstructor)
                    for base in bases
                ):
                    env[statement.name] = _StaticAssetConstructor()
            elif isinstance(statement, ast.Expr):
                self._eval_expr(statement.value, module, env)
            elif isinstance(statement, ast.Return):
                returns.append(
                    self._eval_expr(statement.value, module, env)
                )
            elif isinstance(statement, ast.If):
                body_env = dict(env)
                else_env = dict(env)
                self._process_statements(
                    statement.body, module, body_env, returns
                )
                self._process_statements(
                    statement.orelse, module, else_env, returns
                )
                merged: dict[str, Any] = {}
                for name in body_env.keys() | else_env.keys():
                    left = body_env.get(
                        name,
                        _UnknownRouteValue(statement.lineno),
                    )
                    right = else_env.get(
                        name,
                        _UnknownRouteValue(statement.lineno),
                    )
                    if left is right or (
                        isinstance(left, (str, int, bool, type(None)))
                        and type(left) is type(right)
                        and left == right
                    ):
                        merged[name] = left
                        continue
                    resolved_left = self._resolve(left)
                    resolved_right = self._resolve(right)
                    if isinstance(
                        resolved_left,
                        (_RouteObject, _RouteMethod),
                    ) or isinstance(
                        resolved_right,
                        (_RouteObject, _RouteMethod),
                    ):
                        self._unproven(module, statement.lineno)
                    merged[name] = _UnknownRouteValue(statement.lineno)
                env.clear()
                env.update(merged)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                self._process_statements(
                    statement.body, module, env, returns
                )
                self._process_statements(
                    statement.orelse, module, env, returns
                )
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                self._process_statements(
                    statement.body, module, env, returns
                )
            elif isinstance(statement, ast.Try):
                self._process_statements(
                    statement.body, module, env, returns
                )
                for handler in statement.handlers:
                    self._process_statements(
                        handler.body, module, env, returns
                    )
                self._process_statements(
                    statement.orelse, module, env, returns
                )
                self._process_statements(
                    statement.finalbody, module, env, returns
                )

    def _canonical_root(self) -> _RouteObject | None:
        module = "trading_assistant.app.main"
        env = self._evaluate_module(module)
        configured = self._resolve(env.get("app"))
        if isinstance(configured, _RouteObject):
            return configured
        for name in ("create_app", "_create_app"):
            candidate = self._resolve(env.get(name))
            if isinstance(candidate, _RouteFactory):
                result = self._factory_call(
                    candidate,
                    ast.Call(
                        func=ast.Name(id=name, ctx=ast.Load()),
                        args=[],
                        keywords=[],
                        lineno=candidate.function.lineno,
                        col_offset=0,
                    ),
                    env,
                )
                if isinstance(result, _RouteObject):
                    return result
        self._unproven(module, 1)
        return None

    def _effective_routes(
        self,
        root_object: _RouteObject,
    ) -> list[tuple[str, str, str, int]]:
        effective: list[tuple[str, str, str, int]] = []

        def visit(
            current: _RouteObject,
            inherited: str,
            active: frozenset[str],
        ) -> None:
            if current.key in active:
                self._unproven(
                    current.key.split(":", 1)[0],
                    current.line,
                )
                return
            if current.prefix is None:
                self._unproven(
                    current.key.split(":", 1)[0],
                    current.line,
                )
                return
            composed = _route_join(inherited, current.prefix)
            next_active = active | {current.key}
            module = current.key.rsplit(":", 2)[0]
            relative = self._relative(module)
            for route in current.routes:
                effective.append(
                    (
                        route.method,
                        _route_join(composed, route.path),
                        relative,
                        route.line,
                    )
                )
            for include in current.includes:
                if include.prefix is None:
                    self._unproven(module, include.line)
                    continue
                include_path = _route_join(composed, include.prefix)
                if include.mount:
                    effective.append(
                        ("MOUNT", include_path, relative, include.line)
                    )
                    if include.child is not None:
                        visit(include.child, include_path, next_active)
                    elif not (
                        include.static_assets
                        and include_path == "/static"
                    ):
                        self._unproven(module, include.line)
                elif include.child is None:
                    self._unproven(module, include.line)
                else:
                    visit(include.child, include_path, next_active)

        visit(root_object, "", frozenset())
        return effective

    def scan(self) -> list[ReleaseViolation]:
        root_object = self._canonical_root()
        for possible_root in self._objects:
            for _method, possible_path, relative, line in self._effective_routes(
                possible_root
            ):
                if re.search(
                    r"(?:^/|/)(?:webhook|hooks)[^/]*",
                    possible_path,
                ):
                    self.findings.append(
                        _finding(
                            "WEBHOOK_ROUTE_PRESENT",
                            relative,
                            line,
                        )
                    )
        if root_object is None:
            return self.findings
        effective = self._effective_routes(root_object)
        seen: dict[tuple[str, str], tuple[str, int]] = {}
        for method, path, relative, line in effective:
            if re.search(r"(?:^/|/)(?:webhook|hooks)[^/]*", path):
                self.findings.append(
                    _finding("WEBHOOK_ROUTE_PRESENT", relative, line)
                )
            if method == "MOUNT":
                if path != "/static":
                    self.findings.append(
                        _finding(
                            "ROUTE_REGISTRATION_UNPROVEN",
                            relative,
                            line,
                        )
                    )
                continue
            key = (method, _effective_route_shape(path))
            if key in seen:
                self.findings.append(
                    _finding("DUPLICATE_EFFECTIVE_ROUTE", relative, line)
                )
            else:
                seen[key] = (relative, line)

        policy_relative = "src/trading_assistant/app/policy.py"
        policy_path = self.root / policy_relative
        try:
            policies = _literal_route_policies(self.root)
        except (OSError, SyntaxError, RuntimeError):
            self.findings.append(
                _finding("ROUTE_REGISTRATION_UNPROVEN", policy_relative, 1)
            )
        else:
            for method, path, relative, line in effective:
                if method != "MOUNT" and (method, path) not in policies:
                    self.findings.append(
                        _finding(
                            "ROUTE_REGISTRATION_UNPROVEN",
                            relative,
                            line,
                        )
                    )
            if not policy_path.exists():
                self.findings.append(
                    _finding(
                        "ROUTE_REGISTRATION_UNPROVEN",
                        policy_relative,
                        1,
                    )
                )
        return self.findings


def _scan_effective_route_graph(root: Path) -> list[ReleaseViolation]:
    return _EffectiveRouteGraph(root).scan()


def _is_llm_backend_module(module: str | None) -> bool:
    return module in _LLM_BACKEND_MODULE_PATHS


def _resolved_import_from_modules(
    node: ast.ImportFrom,
    path: Path,
    source_root: Path,
) -> tuple[str, ...]:
    if node.level:
        package = path.relative_to(source_root).parent.parts
        if node.level > len(package):
            return ()
        keep = len(package) - node.level + 1
        base = package[:keep]
        if node.module is not None:
            base += tuple(node.module.split("."))
    elif node.module is not None:
        base = tuple(node.module.split("."))
    else:
        return ()

    return (
        ".".join(base),
        *(
            ".".join((*base, imported.name))
            for imported in node.names
            if imported.name != "*"
        ),
    )


def _global_backend_lookup(node: ast.AST) -> bool:
    if (
        not isinstance(node, ast.Call)
        or not isinstance(node.func, ast.Subscript)
    ):
        return False
    lookup = node.func
    if (
        not isinstance(lookup.slice, ast.Constant)
        or lookup.slice.value not in _LLM_BACKEND_CLASSES
        or not isinstance(lookup.value, ast.Call)
        or not isinstance(lookup.value.func, ast.Name)
    ):
        return False
    return lookup.value.func.id in {"globals", "locals"}


def _check_llm_escape_paths(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    helper_references: list[str] = []
    delegate_references: list[str] = []
    for path in sorted(runtime.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        if relative == _LLM_FACTORY_PATH:
            exposed = next(
                (
                    node
                    for node in tree.body
                    if isinstance(
                        node,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                    and node.name in _RAW_LLM_FACTORY_HELPERS
                ),
                None,
            )
            if exposed is not None:
                _fail(
                    f"raw LLM constructor helper exposed by factory: "
                    f"{relative}:{exposed.lineno}"
                )
        else:
            for node in ast.walk(tree):
                location = (
                    f"{relative}:{node.lineno}"
                    if hasattr(node, "lineno")
                    else None
                )
                if location is None:
                    continue
                if isinstance(node, ast.ImportFrom) and any(
                    imported.name in _RAW_LLM_FACTORY_HELPERS
                    for imported in node.names
                ):
                    helper_references.append(location)
                elif (
                    isinstance(node, ast.Name)
                    and isinstance(node.ctx, ast.Load)
                    and node.id in _RAW_LLM_FACTORY_HELPERS
                ) or (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.ctx, ast.Load)
                    and node.attr in _RAW_LLM_FACTORY_HELPERS
                ) or _getattr_call(
                    node,
                    _RAW_LLM_FACTORY_HELPERS,
                    dynamic=False,
                ):
                    helper_references.append(location)
        if relative in {_LLM_FACTORY_PATH, _LLM_WRAPPER_PATH}:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in _DIRECT_LLM_DELEGATE_ATTRIBUTES
            ) or _getattr_call(
                node,
                _DIRECT_LLM_DELEGATE_ATTRIBUTES,
                dynamic=False,
            ):
                delegate_references.append(
                    f"{relative}:{node.lineno}"
                )
    if helper_references:
        _fail(
            "raw LLM factory helper reference outside factory: "
            + ", ".join(sorted(set(helper_references)))
        )
    if delegate_references:
        _fail(
            "direct LLM delegate access outside wrapper: "
            + ", ".join(sorted(set(delegate_references)))
        )


def _check_llm_construction_paths(root: Path) -> None:
    source_root = root / "src"
    runtime = source_root / "trading_assistant"
    wildcard_imports: list[str] = []
    module_imports: list[str] = []
    references: list[str] = []
    for path in sorted(runtime.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in _LLM_BACKEND_ALLOWED_PATHS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if not hasattr(node, "lineno"):
                continue
            location = f"{relative}:{node.lineno}"
            if isinstance(node, ast.ImportFrom) and any(
                imported.name == "*" for imported in node.names
            ):
                wildcard_imports.append(location)
                continue
            imported_module = (
                isinstance(node, ast.Import)
                and any(
                    _is_llm_backend_module(imported.name)
                    for imported in node.names
                )
            ) or (
                isinstance(node, ast.ImportFrom)
                and any(
                    _is_llm_backend_module(module)
                    for module in _resolved_import_from_modules(
                        node,
                        path,
                        source_root,
                    )
                )
            )
            if imported_module:
                module_imports.append(location)
                continue
            imported_backend = (
                isinstance(node, ast.ImportFrom)
                and any(
                    imported.name in _LLM_BACKEND_CLASSES
                    or imported.asname in _LLM_BACKEND_CLASSES
                    for imported in node.names
                )
            )
            raw_reference = (
                isinstance(node, ast.Name)
                and node.id in _LLM_BACKEND_CLASSES
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr in _LLM_BACKEND_CLASSES
            ) or _getattr_call(
                node,
                _LLM_BACKEND_CLASSES,
                dynamic=False,
            ) or _global_backend_lookup(node)
            if imported_backend or raw_reference:
                references.append(location)
    if wildcard_imports:
        _fail(
            "unproven wildcard import: "
            + ", ".join(sorted(set(wildcard_imports)))
        )
    if module_imports:
        _fail(
            "raw LLM backend module import outside factory: "
            + ", ".join(sorted(set(module_imports)))
        )
    if references:
        _fail(
            "raw LLM backend reference outside factory: "
            + ", ".join(sorted(set(references)))
        )


def _is_deleted_rate_limiter_module(module: str | None) -> bool:
    return module in {"ratelimit", "trading_assistant.app.ratelimit"}


def _check_no_deleted_rate_limiter_import(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    module_imports: list[str] = []
    class_imports: list[str] = []
    references: list[str] = []
    for path in sorted(runtime.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Import)
                and any(
                    _is_deleted_rate_limiter_module(imported.name)
                    for imported in node.names
                )
            ):
                module_imports.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (
                node.module == "trading_assistant.app"
                and any(
                    imported.name == "ratelimit"
                    for imported in node.names
                )
            ):
                module_imports.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (
                _is_deleted_rate_limiter_module(node.module)
                or any(
                    imported.name == "RateLimiter"
                    or imported.asname == "RateLimiter"
                    for imported in node.names
                )
            ):
                class_imports.append(f"{relative}:{node.lineno}")
            elif (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id == "RateLimiter"
            ) or (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr == "RateLimiter"
            ):
                references.append(f"{relative}:{node.lineno}")
    if module_imports:
        _fail(
            "deleted RateLimiter module import: "
            + ", ".join(sorted(set(module_imports)))
        )
    if class_imports:
        _fail(
            "deleted RateLimiter import: "
            + ", ".join(sorted(set(class_imports)))
        )
    if references:
        _fail(
            "deleted RateLimiter reference: "
            + ", ".join(sorted(set(references)))
        )


def _check_encrypted_operational_backup_surface(root: Path) -> None:
    relative = "src/trading_assistant/ops/backup.py"
    path = root / relative
    if path.exists():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        backup_function = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "backup_database"
            ),
            None,
        )
        if backup_function is None:
            _fail("plaintext operational backup entrypoint: missing encrypted backup")
        encrypted_call = any(
            isinstance(node, ast.Call)
            and (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "create_encrypted_database_backup"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "create_encrypted_database_backup"
                )
            )
            and any(
                keyword.arg == "artifact_label"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "whole-database-v1"
                for keyword in node.keywords
            )
            for node in ast.walk(backup_function)
        )
        plaintext_target = any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.lower().endswith((".sqlite", ".sqlite3", ".db"))
            for node in ast.walk(backup_function)
        )
        direct_sqlite_target = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
            and node.func.attr == "connect"
            for node in ast.walk(backup_function)
        )
        if not encrypted_call or plaintext_target or direct_sqlite_target:
            _fail(
                "plaintext operational backup entrypoint: "
                f"{relative}:{backup_function.lineno}"
            )

    required_marker = "whole-database-v1.sqlite3.aesgcm"
    operational_surfaces = (
        "README.md",
        "scripts/launchd/install.sh",
        "scripts/launchd/README.md",
        "docs/RUNBOOK.md",
        "docs/ops/README.md",
    )
    forbidden = (
        re.compile(r"--destination\s+['\"]?\$PROJ/backups(?:['\"\s]|$)"),
        re.compile(r"trading-assistant-\*\.sqlite3"),
        re.compile(r"sqlite3\s+['\"]?\$backup_file"),
    )
    for surface in operational_surfaces:
        candidate = root / surface
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8")
        if (
            surface != "scripts/launchd/install.sh"
            and required_marker not in text
        ) or any(pattern.search(text) for pattern in forbidden):
            _fail(f"plaintext operational backup entrypoint: {surface}")


_APPROVED_PROVIDER_ORIGINS = {
    "alpaca_trading": "https://paper-api.alpaca.markets",
    "alpaca_data": "https://data.alpaca.markets",
    "alpaca_stream": "wss://stream.data.alpaca.markets",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "groq": "https://api.groq.com",
    "telegram": "https://api.telegram.org",
    "coingecko": "https://api.coingecko.com",
}
_APPROVED_OUTBOUND_RULES = {
    "alpaca_trading": (
        "alpaca.trading",
        frozenset(
            {
                "app",
                "daemon",
                "mcp",
                "paper-drill",
                "preflight",
                "safety-drill",
                "watchdog",
            }
        ),
        None,
    ),
    "alpaca_data": (
        "alpaca.historical",
        frozenset(
            {
                "app",
                "daemon",
                "mcp",
                "paper-drill",
                "preflight",
                "safety-drill",
                "validate-analyst",
            }
        ),
        None,
    ),
    "alpaca_stream": (
        "alpaca.stream",
        frozenset({"daemon"}),
        "daemon.use_websocket",
    ),
    "anthropic": (
        "llm.anthropic",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=anthropic",
    ),
    "gemini": (
        "llm.gemini",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=gemini",
    ),
    "groq": (
        "llm.groq",
        frozenset({"app", "daemon", "validate-analyst"}),
        "llm.provider=groq",
    ),
    "telegram": (
        "notifier.telegram",
        frozenset({"app", "daemon", "preflight", "watchdog"}),
        "features.telegram_notifications",
    ),
    "coingecko": (
        "marketdata.coingecko",
        frozenset({"app", "daemon"}),
        "crypto_risk",
    ),
}
_APPROVED_OUTBOUND_ORIGINS = frozenset(
    _APPROVED_PROVIDER_ORIGINS.values()
)
_LOCAL_LIVENESS_URL = "https://localhost:8020/health/live"
_APPROVED_ORIGINS_BY_ADAPTER_PATH = {
    "src/trading_assistant/broker/alpaca.py": frozenset(
        {
            _APPROVED_PROVIDER_ORIGINS["alpaca_trading"],
            _APPROVED_PROVIDER_ORIGINS["alpaca_data"],
            _APPROVED_PROVIDER_ORIGINS["alpaca_stream"],
        }
    ),
    "src/trading_assistant/analyst/news.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["alpaca_data"]}
    ),
    "src/trading_assistant/backtest/data.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["alpaca_data"]}
    ),
    "src/trading_assistant/llm/anthropic_backend.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["anthropic"]}
    ),
    "src/trading_assistant/llm/gemini_backend.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["gemini"]}
    ),
    "src/trading_assistant/llm/groq_backend.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["groq"]}
    ),
    "src/trading_assistant/notifications/telegram.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["telegram"]}
    ),
    "src/trading_assistant/backtest/coingecko.py": frozenset(
        {_APPROVED_PROVIDER_ORIGINS["coingecko"]}
    ),
}
_QUERY_SECRET_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "credential",
        "secret",
        "token",
    }
)
_OUTBOUND_CLIENT_CONSTRUCTORS = frozenset(
    {
        "Client",
        "AsyncClient",
        "ClientSession",
        "OpenAI",
        "AsyncOpenAI",
        "PoolManager",
        "ProxyManager",
        "Session",
        "HTTPConnection",
        "HTTPSConnection",
    }
)
_OUTBOUND_REQUEST_METHODS = frozenset(
    {
        "delete",
        "get",
        "head",
        "options",
        "patch",
        "post",
        "put",
        "request",
        "stream",
        "urlopen",
        "ws_connect",
    }
)
_PROVIDER_CLIENT_IMPORTS = frozenset(
    {
        "Anthropic",
        "CryptoHistoricalDataClient",
        "Groq",
        "NewsClient",
        "StockHistoricalDataClient",
        "TradingClient",
    }
)
_APPROVED_PROVIDER_CLIENT_PATHS = {
    "Anthropic": frozenset(
        {"src/trading_assistant/llm/anthropic_backend.py"}
    ),
    "Groq": frozenset(
        {"src/trading_assistant/llm/groq_backend.py"}
    ),
    "NewsClient": frozenset(
        {"src/trading_assistant/analyst/news.py"}
    ),
    "StockHistoricalDataClient": frozenset(
        {
            "src/trading_assistant/backtest/data.py",
            "src/trading_assistant/broker/alpaca.py",
        }
    ),
    "CryptoHistoricalDataClient": frozenset(
        {"src/trading_assistant/broker/alpaca.py"}
    ),
    "TradingClient": frozenset(
        {"src/trading_assistant/broker/alpaca.py"}
    ),
}


def _yaml_key_line(path: Path, key: str) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 1
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:")
    for number, line in enumerate(lines, 1):
        if pattern.match(line):
            return number
    return 1


def _python_sources(root: Path) -> list[Path]:
    source_root = root / "src" / "trading_assistant"
    if not source_root.exists():
        return []
    return sorted(source_root.rglob("*.py"))


def _literal_string(
    node: ast.AST | None,
    constants: dict[str, str] | None = None,
) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and constants is not None:
        return constants.get(node.id)
    return None


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in getattr(tree, "body", ()):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = _literal_string(node.value, constants)
        if value is None:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value
    return constants


def _literal_frozenset(node: ast.AST | None) -> frozenset[str] | None:
    collection = node
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        collection = node.args[0]
    if not isinstance(collection, (ast.Set, ast.Tuple, ast.List)):
        return None
    values: set[str] = set()
    for element in collection.elts:
        value = _literal_string(element)
        if value is None:
            return None
        values.add(value)
    return frozenset(values)


def _scan_outbound_manifest(root: Path) -> list[ReleaseViolation]:
    relative = "src/trading_assistant/security/outbound.py"
    path = root / relative
    if not path.exists():
        return [_finding("OUTBOUND_ORIGIN_UNAPPROVED", relative, 1)]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return [_finding("OUTBOUND_ORIGIN_UNAPPROVED", relative, 1)]
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "OUTBOUND_ORIGIN_MANIFEST"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ),
        None,
    )
    if assignment is None or not isinstance(
        assignment.value,
        (ast.Tuple, ast.List),
    ):
        return [
            _finding(
                "OUTBOUND_ORIGIN_UNAPPROVED",
                relative,
                getattr(assignment, "lineno", 1),
            )
        ]
    parsed: dict[
        str,
        tuple[str, str, frozenset[str], str | None],
    ] = {}
    for element in assignment.value.elts:
        if not (
            isinstance(element, ast.Call)
            and isinstance(element.func, ast.Name)
            and element.func.id == "OutboundOriginRule"
            and 4 <= len(element.args) <= 5
            and not element.keywords
        ):
            return [
                _finding(
                    "OUTBOUND_ORIGIN_UNAPPROVED",
                    relative,
                    getattr(element, "lineno", assignment.lineno),
                )
            ]
        key = _literal_string(element.args[0])
        adapter = _literal_string(element.args[1])
        origin = _literal_string(element.args[2])
        roles = _literal_frozenset(element.args[3])
        feature = (
            _literal_string(element.args[4])
            if len(element.args) == 5
            else None
        )
        if (
            key is None
            or adapter is None
            or origin is None
            or roles is None
            or not roles
            or key in parsed
            or (
                len(element.args) == 5
                and feature is None
            )
        ):
            return [
                _finding(
                    "OUTBOUND_ORIGIN_UNAPPROVED",
                    relative,
                    element.lineno,
                )
            ]
        parsed[key] = (origin, adapter, roles, feature)
    expected = {
        key: (
            _APPROVED_PROVIDER_ORIGINS[key],
            adapter,
            roles,
            feature,
        )
        for key, (adapter, roles, feature) in _APPROVED_OUTBOUND_RULES.items()
    }
    if parsed != expected:
        return [
            _finding(
                "OUTBOUND_ORIGIN_UNAPPROVED",
                relative,
                assignment.lineno,
            )
        ]
    return []


def _literal_false_annotation(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    annotation_name = (
        node.value.id
        if isinstance(node.value, ast.Name)
        else node.value.attr
        if isinstance(node.value, ast.Attribute)
        else None
    )
    return (
        annotation_name == "Literal"
        and isinstance(node.slice, ast.Constant)
        and node.slice.value is False
    )


def _literal_string_collection(
    node: ast.AST | None,
) -> tuple[frozenset[str], bool]:
    if not isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return frozenset(), False
    values: set[str] = set()
    for element in node.elts:
        value = _literal_string(element)
        if value is None:
            return frozenset(), False
        values.add(value)
    return frozenset(values), True


def _scan_transport_and_integrations(root: Path) -> list[ReleaseViolation]:
    findings = _scan_outbound_manifest(root)
    config_path = root / "config.yaml"
    if config_path.exists():
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return [_finding("CONFIG_UNPROVEN", "config.yaml", 1)]
        server = raw.get("server")
        if not isinstance(server, dict):
            findings.extend(
                (
                    _finding("TLS_DISABLED", "config.yaml", 1),
                    _finding("INSECURE_COOKIE", "config.yaml", 1),
                    _finding("WILDCARD_HOST_ORIGIN", "config.yaml", 1),
                )
            )
        else:
            if server.get("secure_cookies") is not True:
                findings.append(
                    _finding(
                        "INSECURE_COOKIE",
                        "config.yaml",
                        _yaml_key_line(config_path, "secure_cookies"),
                    )
                )
            allowed_hosts = server.get("allowed_hosts")
            origin = server.get("origin")
            if (
                not isinstance(allowed_hosts, list)
                or set(allowed_hosts)
                != {"localhost", "127.0.0.1", "::1"}
            ) or (
                isinstance(origin, str)
                and "*" in origin
            ):
                findings.append(
                    _finding(
                        "WILDCARD_HOST_ORIGIN",
                        "config.yaml",
                        _yaml_key_line(
                            config_path,
                            "allowed_hosts",
                        ),
                    )
                )
            if (
                server.get("bind_host") != "127.0.0.1"
                or server.get("port") != 8020
                or origin != "https://localhost:8020"
                or server.get("tls_ca_path")
                != ".local/tls/rootCA.pem"
                or server.get("tls_cert_path")
                != ".local/tls/localhost.pem"
                or server.get("tls_key_path")
                != ".local/tls/localhost-key.pem"
            ):
                findings.append(
                    _finding(
                        "TLS_DISABLED",
                        "config.yaml",
                        _yaml_key_line(config_path, "origin"),
                    )
                )
            if any(
                server.get(key) not in (None, False, "", ())
                for key in (
                    "proxy_headers",
                    "trust_proxy_headers",
                    "forwarded_allow_ips",
                )
            ):
                findings.append(
                    _finding(
                        "PROXY_HEADERS_TRUSTED",
                        "config.yaml",
                        min(
                            (
                                _yaml_key_line(config_path, key)
                                for key in (
                                    "proxy_headers",
                                    "trust_proxy_headers",
                                    "forwarded_allow_ips",
                                )
                                if key in server
                            ),
                            default=1,
                        ),
                    )
                )
        integrations = raw.get("integrations")
        if not isinstance(integrations, dict):
            findings.extend(
                (
                    _finding("COMPOSIO_ENABLED", "config.yaml", 1),
                    _finding("WEBHOOK_ROUTE_PRESENT", "config.yaml", 1),
                )
            )
        else:
            if integrations.get("composio_enabled") is not False:
                findings.append(
                    _finding(
                        "COMPOSIO_ENABLED",
                        "config.yaml",
                        _yaml_key_line(config_path, "composio_enabled"),
                    )
                )
            if integrations.get("webhooks_enabled") is not False:
                findings.append(
                    _finding(
                        "WEBHOOK_ROUTE_PRESENT",
                        "config.yaml",
                        _yaml_key_line(config_path, "webhooks_enabled"),
                    )
                )
        origins = raw.get("provider_origins")
        if not isinstance(origins, dict):
            findings.append(
                _finding(
                    "OUTBOUND_ORIGIN_UNAPPROVED",
                    "config.yaml",
                    1,
                )
            )
        else:
            for key, value in origins.items():
                if _APPROVED_PROVIDER_ORIGINS.get(key) != value:
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            "config.yaml",
                            _yaml_key_line(config_path, str(key)),
                        )
                    )
            for key in _APPROVED_PROVIDER_ORIGINS:
                if key not in origins:
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            "config.yaml",
                            _yaml_key_line(config_path, "provider_origins"),
                        )
                    )

    config_source = root / "src" / "trading_assistant" / "config.py"
    if not config_source.exists():
        relative = "src/trading_assistant/config.py"
        findings.extend(
            (
                _finding("COMPOSIO_ENABLED", relative, 1),
                _finding("WEBHOOK_ROUTE_PRESENT", relative, 1),
            )
        )
    else:
        relative = config_source.relative_to(root).as_posix()
        try:
            tree = ast.parse(
                config_source.read_text(encoding="utf-8"),
                filename=relative,
            )
        except (OSError, SyntaxError):
            findings.append(_finding("CONFIG_UNPROVEN", relative, 1))
        else:
            integration_class = next(
                (
                    node
                    for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and node.name == "IntegrationsConfig"
                ),
                None,
            )
            if integration_class is None:
                findings.extend(
                    (
                        _finding("COMPOSIO_ENABLED", relative, 1),
                        _finding("WEBHOOK_ROUTE_PRESENT", relative, 1),
                    )
                )
            else:
                values = {
                    node.target.id: node
                    for node in integration_class.body
                    if isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                }
                for name, code in (
                    ("composio_enabled", "COMPOSIO_ENABLED"),
                    ("webhooks_enabled", "WEBHOOK_ROUTE_PRESENT"),
                ):
                    declaration = values.get(name)
                    if (
                        declaration is None
                        or not isinstance(declaration.value, ast.Constant)
                        or declaration.value.value is not False
                        or not _literal_false_annotation(
                            declaration.annotation
                        )
                    ):
                        findings.append(
                            _finding(
                                code,
                                relative,
                                getattr(declaration, "lineno", integration_class.lineno),
                            )
                        )

    env_example = root / ".env.example"
    if env_example.exists():
        try:
            lines = env_example.read_text(encoding="utf-8").splitlines()
        except OSError:
            findings.append(_finding("CONFIG_UNPROVEN", ".env.example", 1))
        else:
            for line_number, line in enumerate(lines, 1):
                name = line.split("=", 1)[0].strip().upper()
                if name.startswith("COMPOSIO_"):
                    findings.append(
                        _finding(
                            "COMPOSIO_ENABLED",
                            ".env.example",
                            line_number,
                        )
                    )

    for path in _python_sources(root):
        relative = path.relative_to(root).as_posix()
        relative_parts = PurePosixPath(relative).parts
        composio_sensitive_path = (
            "mcp_server" in relative_parts
            or "integration" in path.stem.lower()
            or "toolkit" in path.stem.lower()
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue
        transport_mapping_entries = _mapping_alias_entries(tree)
        ssl_module_aliases = {"ssl"}
        ssl_context_factories = {"create_default_context"}
        ssl_unverified_factories = {"_create_unverified_context"}
        ssl_contexts: set[str] = set()
        cookie_callables: set[str] = set()
        cors_middleware_names = {"CORSMiddleware"}
        trusted_host_middleware_names = {"TrustedHostMiddleware"}
        uvicorn_module_names: set[str] = set()
        uvicorn_run_names: set[str] = set()
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.Import):
                for imported in candidate.names:
                    if imported.name == "ssl":
                        ssl_module_aliases.add(imported.asname or "ssl")
                    if imported.name == "uvicorn":
                        uvicorn_module_names.add(
                            imported.asname or "uvicorn"
                        )
            elif (
                isinstance(candidate, ast.ImportFrom)
                and candidate.module == "ssl"
            ):
                for imported in candidate.names:
                    if imported.name == "create_default_context":
                        ssl_context_factories.add(
                            imported.asname or imported.name
                        )
                    if imported.name == "_create_unverified_context":
                        ssl_unverified_factories.add(
                            imported.asname or imported.name
                        )
            elif isinstance(candidate, ast.ImportFrom):
                for imported in candidate.names:
                    local = imported.asname or imported.name
                    if imported.name == "CORSMiddleware":
                        cors_middleware_names.add(local)
                    if imported.name == "TrustedHostMiddleware":
                        trusted_host_middleware_names.add(local)
                    if (
                        candidate.module == "uvicorn"
                        and imported.name == "run"
                    ):
                        uvicorn_run_names.add(local)

        changed = True
        while changed:
            changed = False
            for candidate in ast.walk(tree):
                if not isinstance(
                    candidate,
                    (ast.Assign, ast.AnnAssign),
                ):
                    continue
                targets = [
                    target.id
                    for target in _assignment_targets(candidate)
                    if isinstance(target, ast.Name)
                ]
                if not targets:
                    continue
                value = candidate.value
                value_path = _attribute_path(value)
                if (
                    len(value_path) == 2
                    and value_path[0] in ssl_module_aliases
                    and value_path[1] == "create_default_context"
                ) or (
                    isinstance(value, ast.Name)
                    and value.id in ssl_context_factories
                ):
                    before = len(ssl_context_factories)
                    ssl_context_factories.update(targets)
                    changed = (
                        changed
                        or len(ssl_context_factories) != before
                    )
                if (
                    len(value_path) == 2
                    and value_path[0] in ssl_module_aliases
                    and value_path[1] == "_create_unverified_context"
                ) or (
                    isinstance(value, ast.Name)
                    and value.id in ssl_unverified_factories
                ):
                    before = len(ssl_unverified_factories)
                    ssl_unverified_factories.update(targets)
                    changed = (
                        changed
                        or len(ssl_unverified_factories) != before
                    )
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "set_cookie"
                ) or (
                    isinstance(value, ast.Name)
                    and value.id in cookie_callables
                ):
                    before = len(cookie_callables)
                    cookie_callables.update(targets)
                    changed = changed or len(cookie_callables) != before
                if (
                    isinstance(value, ast.Name)
                    and value.id in cors_middleware_names
                ):
                    before = len(cors_middleware_names)
                    cors_middleware_names.update(targets)
                    changed = (
                        changed or len(cors_middleware_names) != before
                    )
                if (
                    isinstance(value, ast.Name)
                    and value.id in trusted_host_middleware_names
                ):
                    before = len(trusted_host_middleware_names)
                    trusted_host_middleware_names.update(targets)
                    changed = (
                        changed
                        or len(trusted_host_middleware_names) != before
                    )
                if (
                    isinstance(value, ast.Attribute)
                    and len(value_path) == 2
                    and value_path[0] in uvicorn_module_names
                    and value_path[1] == "run"
                ) or (
                    isinstance(value, ast.Name)
                    and value.id in uvicorn_run_names
                ):
                    before = len(uvicorn_run_names)
                    uvicorn_run_names.update(targets)
                    changed = changed or len(uvicorn_run_names) != before

        for candidate in ast.walk(tree):
            if isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                value = candidate.value
                if not isinstance(value, ast.Call):
                    continue
                function_path = _attribute_path(value.func)
                is_context_factory = (
                    isinstance(value.func, ast.Name)
                    and value.func.id in ssl_context_factories
                ) or (
                    len(function_path) == 2
                    and function_path[0] in ssl_module_aliases
                    and function_path[1] == "create_default_context"
                )
                if is_context_factory:
                    ssl_contexts.update(
                        target.id
                        for target in _assignment_targets(candidate)
                        if isinstance(target, ast.Name)
                    )
        changed = True
        while changed:
            changed = False
            for candidate in ast.walk(tree):
                if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                    continue
                if not isinstance(candidate.value, ast.Name):
                    continue
                if candidate.value.id not in ssl_contexts:
                    continue
                before = len(ssl_contexts)
                ssl_contexts.update(
                    target.id
                    for target in _assignment_targets(candidate)
                    if isinstance(target, ast.Name)
                )
                changed = changed or len(ssl_contexts) != before
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(
                node.value,
                str,
            ):
                parsed = urlsplit(node.value)
                if (
                    parsed.scheme.lower() in {"http", "https", "ws", "wss"}
                    and parsed.hostname is not None
                    and "composio" in parsed.hostname.lower()
                ):
                    findings.append(
                        _finding(
                            "COMPOSIO_ENABLED",
                            relative,
                            node.lineno,
                        )
                    )
            composio_import = (
                isinstance(node, ast.Import)
                and any(
                    imported.name == "composio"
                    or imported.name.startswith("composio.")
                    for imported in node.names
                )
            ) or (
                isinstance(node, ast.ImportFrom)
                and isinstance(node.module, str)
                and (
                    node.module == "composio"
                    or node.module.startswith("composio.")
                )
            )
            if composio_import:
                findings.append(
                    _finding("COMPOSIO_ENABLED", relative, node.lineno)
                )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = _assignment_targets(node)
                for target in targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in ssl_contexts
                    ):
                        continue
                    value_path = _attribute_path(node.value)
                    if target.attr == "verify_mode" and not (
                        len(value_path) == 2
                        and value_path[0] in ssl_module_aliases
                        and value_path[1] == "CERT_REQUIRED"
                    ):
                        findings.append(
                            _finding("TLS_DISABLED", relative, node.lineno)
                        )
                    elif target.attr == "check_hostname" and not (
                        isinstance(node.value, ast.Constant)
                        and node.value.value is True
                    ):
                        findings.append(
                            _finding("TLS_DISABLED", relative, node.lineno)
                        )
                    elif target.attr == "minimum_version":
                        allowed_minimums = {
                            (alias, "TLSVersion", version)
                            for alias in ssl_module_aliases
                            for version in {"TLSv1_2", "TLSv1_3"}
                        }
                        if value_path not in allowed_minimums:
                            findings.append(
                                _finding(
                                    "TLS_DISABLED",
                                    relative,
                                    node.lineno,
                                )
                            )
            if isinstance(node, ast.Call):
                raw_call_name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else node.func.id
                    if isinstance(node.func, ast.Name)
                    else ""
                )
                call_path = _attribute_path(node.func)
                call_name = (
                    "set_cookie"
                    if isinstance(node.func, ast.Name)
                    and node.func.id in cookie_callables
                    else "CORSMiddleware"
                    if isinstance(node.func, ast.Name)
                    and node.func.id in cors_middleware_names
                    else "TrustedHostMiddleware"
                    if isinstance(node.func, ast.Name)
                    and node.func.id in trusted_host_middleware_names
                    else raw_call_name
                )
                if call_name == "add_middleware" and node.args:
                    middleware = node.args[0]
                    middleware_name = (
                        middleware.id
                        if isinstance(middleware, ast.Name)
                        else middleware.attr
                        if isinstance(middleware, ast.Attribute)
                        else ""
                    )
                    if middleware_name in cors_middleware_names:
                        call_name = "CORSMiddleware"
                    elif middleware_name in trusted_host_middleware_names:
                        call_name = "TrustedHostMiddleware"
                effective_keywords = [
                    keyword
                    for keyword in node.keywords
                    if keyword.arg is not None
                ]
                unpacked_dynamic = False
                for keyword in node.keywords:
                    if keyword.arg is not None:
                        continue
                    if isinstance(keyword.value, ast.Dict):
                        entries, dynamic = _mapping_literal_entries(
                            keyword.value
                        )
                    elif (
                        isinstance(keyword.value, ast.Name)
                        and keyword.value.id in transport_mapping_entries
                    ):
                        entries, dynamic = transport_mapping_entries[
                            keyword.value.id
                        ]
                    else:
                        entries, dynamic = {}, True
                    effective_keywords.extend(
                        ast.keyword(arg=name, value=value)
                        for name, value in entries.items()
                    )
                    unpacked_dynamic = unpacked_dynamic or dynamic
                unverified_context = (
                    isinstance(node.func, ast.Name)
                    and node.func.id in ssl_unverified_factories
                ) or (
                    len(call_path) == 2
                    and call_path[0] in ssl_module_aliases
                    and call_path[1] == "_create_unverified_context"
                )
                if unverified_context:
                    findings.append(
                        _finding("TLS_DISABLED", relative, node.lineno)
                    )
                if "composio" in call_name.lower():
                    findings.append(
                        _finding(
                            "COMPOSIO_ENABLED",
                            relative,
                            node.lineno,
                        )
                    )
                if (
                    call_name == "getattr"
                    and len(node.args) >= 2
                    and (
                        (
                            _literal_string(node.args[1]) is not None
                            and "composio"
                            in _literal_string(node.args[1]).lower()
                        )
                        or _literal_string(node.args[1]) is None
                        and (
                            "composio" in relative.lower()
                            or composio_sensitive_path
                        )
                    )
                ):
                    findings.append(
                        _finding(
                            "COMPOSIO_ENABLED",
                            relative,
                            node.lineno,
                        )
                    )
                if call_name in {
                    "CORSMiddleware",
                    "TrustedHostMiddleware",
                }:
                    keyword_name = (
                        "allow_origins"
                        if call_name == "CORSMiddleware"
                        else "allowed_hosts"
                    )
                    configured = next(
                        (
                            keyword.value
                            for keyword in effective_keywords
                            if keyword.arg == keyword_name
                        ),
                        None,
                    )
                    values, proven = _literal_string_collection(
                        configured
                    )
                    canonical_trusted_host = (
                        call_name == "TrustedHostMiddleware"
                        and relative
                        == "src/trading_assistant/app/main.py"
                        and isinstance(configured, ast.List)
                        and len(configured.elts) == 1
                        and _attribute_path(configured.elts[0])
                        == ("transport_policy", "canonical_host")
                    )
                    if (
                        "*" in values
                        or not proven
                        and not canonical_trusted_host
                    ):
                        findings.append(
                            _finding(
                                "WILDCARD_HOST_ORIGIN",
                                relative,
                                node.lineno,
                            )
                        )
                    if call_name == "CORSMiddleware":
                        origin_regex = next(
                            (
                                keyword.value
                                for keyword in effective_keywords
                                if keyword.arg == "allow_origin_regex"
                            ),
                            None,
                        )
                        if origin_regex is not None:
                            regex_value = _literal_string(origin_regex)
                            if (
                                regex_value is None
                                or regex_value not in {"", "^$"}
                            ):
                                findings.append(
                                    _finding(
                                        "WILDCARD_HOST_ORIGIN",
                                        relative,
                                        node.lineno,
                                    )
                                )
                if call_name in {
                    "CORSMiddleware",
                    "TrustedHostMiddleware",
                } and unpacked_dynamic:
                    findings.append(
                        _finding(
                            "WILDCARD_HOST_ORIGIN",
                            relative,
                            node.lineno,
                        )
                    )
                if call_name == "set_cookie":
                    cookie_options = {
                        keyword.arg: keyword.value
                        for keyword in effective_keywords
                        if keyword.arg is not None
                    }
                    secure = cookie_options.get("secure")
                    httponly = cookie_options.get("httponly")
                    samesite = _literal_string(
                        cookie_options.get("samesite")
                    )
                    if not (
                        isinstance(secure, ast.Constant)
                        and secure.value is True
                        and isinstance(httponly, ast.Constant)
                        and httponly.value is True
                        and samesite in {"strict", "lax"}
                    ):
                        findings.append(
                            _finding(
                                "INSECURE_COOKIE",
                                relative,
                                node.lineno,
                            )
                        )
                    if unpacked_dynamic:
                        findings.append(
                            _finding(
                                "INSECURE_COOKIE",
                                relative,
                                node.lineno,
                            )
                        )
                uvicorn_run = (
                    isinstance(node.func, ast.Name)
                    and node.func.id in uvicorn_run_names
                ) or (
                    len(call_path) == 2
                    and call_path[0] in uvicorn_module_names
                    and call_path[1] == "run"
                )
                if uvicorn_run:
                    uvicorn_options = {
                        keyword.arg: keyword.value
                        for keyword in effective_keywords
                        if keyword.arg is not None
                    }
                    proxy_headers = uvicorn_options.get("proxy_headers")
                    forwarded = uvicorn_options.get(
                        "forwarded_allow_ips"
                    )
                    if (
                        unpacked_dynamic
                        or not isinstance(proxy_headers, ast.Constant)
                        or proxy_headers.value is not False
                        or not isinstance(forwarded, ast.Constant)
                        or forwarded.value != ""
                    ):
                        findings.append(
                            _finding(
                                "PROXY_HEADERS_TRUSTED",
                                relative,
                                node.lineno,
                            )
                        )
                if call_name in {"import_module", "__import__"} and node.args:
                    module_name = _literal_string(node.args[0])
                    if (
                        module_name is not None
                        and (
                            module_name == "composio"
                            or module_name.startswith("composio.")
                        )
                        or module_name is None
                        and composio_sensitive_path
                    ):
                        findings.append(
                            _finding(
                                "COMPOSIO_ENABLED",
                                relative,
                                node.lineno,
                            )
                        )

    dangerous_doc = re.compile(
        r"\b(?:pip(?:3)?\s+install|uv\s+add|poetry\s+add)\s+composio\b"
        r"|\bcomposio\s+(?:login|connect|install|enable)\b"
        r"|https?://[^\s)]*composio",
        re.IGNORECASE,
    )
    for relative in (
        "README.md",
        "docs/RUNBOOK.md",
        "docs/ops/README.md",
        "scripts/launchd/README.md",
    ):
        path = root / relative
        if not path.exists():
            findings.extend(
                (
                    _finding("COMPOSIO_ENABLED", relative, 1),
                    _finding("WEBHOOK_ROUTE_PRESENT", relative, 1),
                )
            )
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            findings.extend(
                (
                    _finding("COMPOSIO_ENABLED", relative, 1),
                    _finding("WEBHOOK_ROUTE_PRESENT", relative, 1),
                )
            )
            continue
        normalized = " ".join(lines).lower()
        if "composio" not in normalized or "disabled" not in normalized:
            findings.append(
                _finding("COMPOSIO_ENABLED", relative, 1)
            )
        if "no webhook receiver" not in normalized:
            findings.append(
                _finding("WEBHOOK_ROUTE_PRESENT", relative, 1)
            )
        for line_number, line in enumerate(lines, 1):
            if dangerous_doc.search(line):
                findings.append(
                    _finding("COMPOSIO_ENABLED", relative, line_number)
                )
    superpowers_root = root / "docs" / "superpowers"
    if superpowers_root.exists():
        for path in sorted(superpowers_root.rglob("*.md")):
            relative = path.relative_to(root).as_posix()
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                findings.append(
                    _finding("OUTBOUND_ORIGIN_UNAPPROVED", relative, 1)
                )
                continue
            for line_number, line in enumerate(lines, 1):
                normalized = " ".join(line.lower().split())
                if "marketstack" not in normalized:
                    continue
                historical_removal = (
                    "historical non-executable" in normalized
                    and "removed" in normalized
                    and "alpaca historical data" in normalized
                )
                if not historical_removal:
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            relative,
                            line_number,
                        )
                    )
    return findings


def _mapping_literal_keys(node: ast.AST | None) -> tuple[set[str], bool]:
    entries, dynamic = _mapping_literal_entries(node)
    return set(entries), dynamic


def _mapping_literal_entries(
    node: ast.AST | None,
) -> tuple[dict[str, ast.AST], bool]:
    if not isinstance(node, ast.Dict):
        return {}, True
    entries: dict[str, ast.AST] = {}
    dynamic = False
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value.lower()] = value
        else:
            dynamic = True
    return entries, dynamic


def _static_string_fragments(node: ast.AST | None) -> tuple[str, ...]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return (node.value,)
    if isinstance(node, ast.JoinedStr):
        return tuple(
            fragment
            for value in node.values
            for fragment in _static_string_fragments(value)
        )
    if isinstance(node, ast.FormattedValue):
        return ()
    if isinstance(node, ast.BinOp) and isinstance(
        node.op,
        (ast.Add, ast.Mod),
    ):
        return (
            *_static_string_fragments(node.left),
            *_static_string_fragments(node.right),
        )
    return ()


def _mapping_alias_entries(
    tree: ast.AST,
) -> dict[str, tuple[dict[str, ast.AST], bool]]:
    """Resolve mapping aliases as shared objects and union every mutation."""

    parent: dict[str, str] = {}

    def add(name: str) -> None:
        parent.setdefault(name, name)

    def find(name: str) -> str:
        add(name)
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    for node in assignments:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name)
        ]
        if isinstance(node.value, ast.Dict):
            for name in names:
                add(name)
        elif isinstance(node.value, ast.Name):
            for name in names:
                union(name, node.value.id)

    component_entries: dict[str, dict[str, ast.AST]] = {}
    component_dynamic: dict[str, bool] = {}

    def merge(
        name: str,
        entries: dict[str, ast.AST],
        dynamic: bool,
    ) -> None:
        root_name = find(name)
        current = component_entries.setdefault(root_name, {})
        for key, value in entries.items():
            previous = current.get(key)
            if (
                previous is not None
                and ast.dump(previous, include_attributes=False)
                != ast.dump(value, include_attributes=False)
            ):
                component_dynamic[root_name] = True
            current[key] = value
        component_dynamic[root_name] = (
            component_dynamic.get(root_name, False) or dynamic
        )

    for node in assignments:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
        )
        names = [
            target.id
            for target in targets
            if isinstance(target, ast.Name)
        ]
        if isinstance(node.value, ast.Dict):
            entries, dynamic = _mapping_literal_entries(node.value)
            for name in names:
                merge(name, entries, dynamic)
        elif names and not isinstance(node.value, ast.Name):
            for name in names:
                if name in parent:
                    merge(name, {}, True)

        for target in targets:
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id in parent
            ):
                continue
            key = _literal_string(target.slice)
            merge(
                target.value.id,
                {key.lower(): node.value} if key is not None else {},
                key is None,
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id in parent:
                entries, dynamic = _mapping_literal_entries(node.value)
                merge(node.target.id, entries, dynamic)
            elif (
                isinstance(node.target, ast.Subscript)
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id in parent
            ):
                merge(node.target.value.id, {}, True)
        if isinstance(node, ast.Delete):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in parent
                ):
                    merge(target.value.id, {}, True)
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in parent
        ):
            continue
        name = node.func.value.id
        if node.func.attr == "setdefault":
            key = _literal_string(node.args[0] if node.args else None)
            value = node.args[1] if len(node.args) >= 2 else ast.Constant(None)
            merge(
                name,
                {key.lower(): value} if key is not None else {},
                key is None or len(node.args) not in {1, 2} or bool(node.keywords),
            )
        elif node.func.attr == "update":
            entries: dict[str, ast.AST] = {}
            dynamic = False
            if len(node.args) == 1:
                argument = node.args[0]
                if isinstance(argument, ast.Dict):
                    entries, dynamic = _mapping_literal_entries(argument)
                elif isinstance(argument, ast.Name) and argument.id in parent:
                    source_root = find(argument.id)
                    entries = dict(
                        component_entries.get(source_root, {})
                    )
                    dynamic = component_dynamic.get(source_root, False)
                else:
                    dynamic = True
            elif node.args:
                dynamic = True
            for keyword in node.keywords:
                if keyword.arg is None:
                    dynamic = True
                else:
                    entries[keyword.arg.lower()] = keyword.value
            merge(name, entries, dynamic)
        elif node.func.attr in _AUTHORITY_MUTATORS:
            merge(name, {}, True)

    resolved: dict[str, tuple[dict[str, ast.AST], bool]] = {}
    for name in parent:
        root_name = find(name)
        resolved[name] = (
            dict(component_entries.get(root_name, {})),
            component_dynamic.get(root_name, False),
        )
    return resolved


def _mapping_alias_keys(
    tree: ast.AST,
) -> dict[str, tuple[set[str], bool]]:
    return {
        name: (set(entries), dynamic)
        for name, (entries, dynamic) in _mapping_alias_entries(tree).items()
    }


def _scan_outbound_clients(root: Path) -> list[ReleaseViolation]:
    findings: list[ReleaseViolation] = []
    for path in _python_sources(root):
        relative = path.relative_to(root).as_posix()
        boundary_wrapper = (
            relative == "src/trading_assistant/security/outbound.py"
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            findings.append(
                _finding("OUTBOUND_CLIENT_UNAPPROVED", relative, 1)
            )
            continue
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }

        def enclosing_context(
            candidate: ast.AST,
        ) -> tuple[
            ast.FunctionDef | ast.AsyncFunctionDef | None,
            ast.ClassDef | None,
        ]:
            function = None
            class_node = None
            current = parents.get(candidate)
            while current is not None:
                if (
                    function is None
                    and isinstance(
                        current,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                ):
                    function = current
                if isinstance(current, ast.ClassDef):
                    class_node = current
                    break
                current = parents.get(current)
            return function, class_node

        constants = _module_string_constants(tree)
        mapping_entries = _mapping_alias_entries(tree)
        mapping_aliases = {
            name: (set(entries), dynamic)
            for name, (entries, dynamic) in mapping_entries.items()
        }
        verified_tls_contexts: set[str] = set()
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            value = assignment.value
            if not isinstance(value, ast.Call):
                continue
            constructor = _attribute_path(value.func)
            if constructor not in {
                ("ssl", "create_default_context"),
                ("_verified_ssl_context",),
                ("ssl_context_factory",),
            }:
                continue
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            verified_tls_contexts.update(
                target.id
                for target in targets
                if isinstance(target, ast.Name)
            )
        imported_client_names: set[str] = set()
        imported_request_names: set[str] = set()
        provider_client_names: dict[str, str] = {}
        module_aliases: set[str] = set()
        google_genai_aliases: set[str] = set()
        network_modules = {
            "anthropic",
            "http",
            "requests",
            "httpx",
            "aiohttp",
            "openai",
            "urllib3",
            "urllib",
            "websockets",
            "socket",
        }
        network_alias_paths: dict[str, tuple[str, ...]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    root_module = imported.name.split(".", 1)[0]
                    if root_module in network_modules:
                        local = imported.asname or root_module
                        module_aliases.add(local)
                        network_alias_paths[local] = (
                            tuple(imported.name.split("."))
                            if imported.asname
                            else (root_module,)
                        )
            elif (
                isinstance(node, ast.ImportFrom)
                and isinstance(node.module, str)
                and node.module.split(".", 1)[0] in network_modules
            ):
                for imported in node.names:
                    local = imported.asname or imported.name
                    network_alias_paths[local] = (
                        *node.module.split("."),
                        imported.name,
                    )
                    if imported.name in _OUTBOUND_CLIENT_CONSTRUCTORS:
                        imported_client_names.add(local)
                    if (
                        imported.name.lower()
                        in _OUTBOUND_REQUEST_METHODS
                        or imported.name
                        in {"connect", "create_connection"}
                    ):
                        imported_request_names.add(local)
            if isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    local = imported.asname or imported.name
                    if imported.name in _PROVIDER_CLIENT_IMPORTS:
                        provider_client_names[local] = imported.name
                    if (
                        node.module == "google"
                        and imported.name == "genai"
                    ):
                        google_genai_aliases.add(local)

        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                names = {
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name)
                }
                if not names:
                    continue
                value = node.value
                if isinstance(value, ast.Name):
                    if value.id in network_alias_paths:
                        for name in names:
                            alias_path = network_alias_paths[value.id]
                            if network_alias_paths.get(name) != alias_path:
                                network_alias_paths[name] = alias_path
                                changed = True
                    if value.id in imported_client_names:
                        before = len(imported_client_names)
                        imported_client_names.update(names)
                        changed = changed or len(imported_client_names) != before
                    if value.id in imported_request_names:
                        before = len(imported_request_names)
                        imported_request_names.update(names)
                        changed = changed or len(imported_request_names) != before
                    if value.id in provider_client_names:
                        for name in names:
                            if name not in provider_client_names:
                                provider_client_names[name] = (
                                    provider_client_names[value.id]
                                )
                                changed = True
                if (
                    isinstance(value, ast.Attribute)
                ):
                    value_path = _attribute_path(value)
                    canonical_value_path = value_path
                    if (
                        value_path
                        and value_path[0] in network_alias_paths
                    ):
                        canonical_value_path = (
                            *network_alias_paths[value_path[0]],
                            *value_path[1:],
                        )
                    if (
                        canonical_value_path
                        and canonical_value_path[0] in network_modules
                    ):
                        for name in names:
                            if (
                                network_alias_paths.get(name)
                                != canonical_value_path
                            ):
                                network_alias_paths[name] = (
                                    canonical_value_path
                                )
                                changed = True
                    if (
                        canonical_value_path
                        and canonical_value_path[-1]
                        in (
                            _OUTBOUND_CLIENT_CONSTRUCTORS
                            | _PROVIDER_CLIENT_IMPORTS
                        )
                    ):
                        before = len(imported_client_names)
                        imported_client_names.update(names)
                        changed = changed or len(imported_client_names) != before
                    if (
                        canonical_value_path
                        and (
                            canonical_value_path[-1].lower()
                            in _OUTBOUND_REQUEST_METHODS
                            or canonical_value_path[-1]
                            in {"connect", "create_connection"}
                        )
                    ):
                        before = len(imported_request_names)
                        imported_request_names.update(names)
                        changed = changed or len(imported_request_names) != before

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for target in targets:
                    target_name = (
                        target.attr
                        if isinstance(target, ast.Attribute)
                        else _literal_string(target.slice)
                        if isinstance(target, ast.Subscript)
                        else None
                    )
                    if target_name is None:
                        continue
                    if target_name in {
                        "follow_redirects",
                        "allow_redirects",
                    } and (
                        not isinstance(node.value, ast.Constant)
                        or node.value.value is not False
                    ):
                        findings.append(
                            _finding(
                                "CROSS_ORIGIN_REDIRECT_ENABLED",
                                relative,
                                node.lineno,
                            )
                        )
                    if target_name in {
                        "trust_env",
                        "proxy",
                        "proxies",
                        "forwarded_allow_ips",
                    } and (
                        not isinstance(node.value, ast.Constant)
                        or node.value.value not in {None, False, ""}
                    ) and not (
                        target_name in {"proxy", "proxies"}
                        and isinstance(node.value, ast.Dict)
                        and not node.value.keys
                    ):
                        findings.append(
                            _finding(
                                "PROXY_HEADERS_TRUSTED",
                                relative,
                                node.lineno,
                            )
                        )
            if not isinstance(node, ast.Call):
                continue
            call_path = _attribute_path(node.func)
            canonical_call_path = call_path
            if call_path and call_path[0] in network_alias_paths:
                canonical_call_path = (
                    *network_alias_paths[call_path[0]],
                    *call_path[1:],
                )
            direct_client = (
                isinstance(node.func, ast.Name)
                and node.func.id in imported_client_names
            )
            direct_request = (
                isinstance(node.func, ast.Name)
                and node.func.id in imported_request_names
            )
            provider_client_name = (
                provider_client_names.get(node.func.id)
                if isinstance(node.func, ast.Name)
                else "Client"
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in google_genai_aliases
                    and node.func.attr == "Client"
                )
                else None
            )
            provider_client_unapproved = (
                provider_client_name is not None
                and relative
                not in _APPROVED_PROVIDER_CLIENT_PATHS.get(
                    provider_client_name,
                    frozenset(
                        {
                            "src/trading_assistant/llm/gemini_backend.py"
                        }
                    )
                    if provider_client_name == "Client"
                    else frozenset(),
                )
            )
            module_client = (
                bool(canonical_call_path)
                and canonical_call_path[0] in network_modules
                and canonical_call_path[-1] in (
                    _OUTBOUND_CLIENT_CONSTRUCTORS
                    | _PROVIDER_CLIENT_IMPORTS
                )
            )
            module_request = (
                bool(canonical_call_path)
                and canonical_call_path[0] in network_modules
                and (
                    canonical_call_path[-1].lower()
                    in _OUTBOUND_REQUEST_METHODS
                    or canonical_call_path[-1]
                    in {"connect", "create_connection"}
                )
            )
            module_getattr = (
                isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in module_aliases
            )
            module_dunder_getattr = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__getattribute__"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_aliases
            )
            dynamic_network_import = (
                (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "__import__"
                )
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                )
            ) and (
                not node.args
                or _literal_string(node.args[0]) is None
                or _literal_string(node.args[0]).split(".", 1)[0]
                in network_modules
            )
            if (
                (
                    direct_client
                    or (
                        module_client
                        and canonical_call_path[-1]
                        not in _PROVIDER_CLIENT_IMPORTS
                    )
                )
                and not boundary_wrapper
                or direct_request
                or module_request
                or (
                    module_client
                    and canonical_call_path[-1]
                    in _PROVIDER_CLIENT_IMPORTS
                    and relative
                    not in _APPROVED_PROVIDER_CLIENT_PATHS.get(
                        canonical_call_path[-1],
                        frozenset(),
                    )
                )
                or module_getattr
                or module_dunder_getattr
                or dynamic_network_import
                or provider_client_unapproved
            ):
                findings.append(
                    _finding(
                        "OUTBOUND_CLIENT_UNAPPROVED",
                        relative,
                        node.lineno,
                    )
                )

            allowed_for_adapter = _APPROVED_ORIGINS_BY_ADAPTER_PATH.get(
                relative,
                frozenset(),
            )
            call_name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            network_option_call = (
                direct_client
                or direct_request
                or module_client
                or module_request
                or provider_client_name is not None
                or call_name in _OUTBOUND_CLIENT_CONSTRUCTORS
                or call_name.lower() in _OUTBOUND_REQUEST_METHODS
            )
            if (
                boundary_wrapper
                and call_name.lower() in _OUTBOUND_REQUEST_METHODS
                and not module_request
                and not direct_request
            ):
                request_url_node = (
                    node.args[1]
                    if call_name.lower() == "request"
                    and len(node.args) >= 2
                    else node.args[0]
                    if node.args
                    else next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg == "url"
                        ),
                        None,
                    )
                )
                request_url = _literal_string(
                    request_url_node,
                    constants,
                )
                function, class_node = enclosing_context(node)
                approved_dynamic_request = (
                    request_url is None
                    and function is not None
                    and class_node is not None
                    and class_node.name == "NoRedirectSession"
                    and function.name == "request"
                    and call_name.lower() == "request"
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Call)
                    and isinstance(node.func.value.func, ast.Name)
                    and node.func.value.func.id == "super"
                    and any(
                        isinstance(guard, ast.Call)
                        and _attribute_path(guard.func)
                        == ("self", "_policy", "assert_url")
                        and guard.args
                        and isinstance(request_url_node, ast.Name)
                        and isinstance(guard.args[0], ast.Name)
                        and guard.args[0].id == request_url_node.id
                        for guard in ast.walk(function)
                    )
                )
                if request_url is None and not approved_dynamic_request:
                    findings.append(
                        _finding(
                            "OUTBOUND_CLIENT_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )
            effective_keywords = [
                keyword
                for keyword in node.keywords
                if keyword.arg is not None
            ]
            for keyword in node.keywords:
                if keyword.arg is not None or not network_option_call:
                    continue
                if isinstance(keyword.value, ast.Dict):
                    entries, unresolved = _mapping_literal_entries(
                        keyword.value
                    )
                    # Inline unpacking is unsupported even when its contents
                    # are also inspected for a more specific finding.
                    findings.append(
                        _finding(
                            "OUTBOUND_CLIENT_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )
                elif (
                    isinstance(keyword.value, ast.Name)
                    and keyword.value.id in mapping_entries
                ):
                    entries, unresolved = mapping_entries[
                        keyword.value.id
                    ]
                else:
                    entries, unresolved = {}, True
                effective_keywords.extend(
                    ast.keyword(arg=name, value=value)
                    for name, value in entries.items()
                )
                if unresolved:
                    findings.append(
                        _finding(
                            "OUTBOUND_CLIENT_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )
            url_nodes = list(node.args)
            url_nodes.extend(
                keyword.value
                for keyword in effective_keywords
                if keyword.arg is not None
                and keyword.arg.lower()
                in {"url", "base_url", "baseurl", "url_override"}
            )
            for argument in url_nodes:
                value = _literal_string(argument, constants)
                fragments = "".join(_static_string_fragments(argument))
                if re.search(
                    r"(?:\?|&)(?:access_key|api_key|apikey|credential|"
                    r"secret|token)\s*=",
                    fragments,
                    flags=re.IGNORECASE,
                ):
                    findings.append(
                        _finding("QUERY_SECRET", relative, node.lineno)
                    )
                if value is None or "?" not in value:
                    query = ""
                else:
                    try:
                        query = urlsplit(value).query
                    except ValueError:
                        query = ""
                        findings.append(
                            _finding(
                                "OUTBOUND_ORIGIN_UNAPPROVED",
                                relative,
                                node.lineno,
                            )
                        )
                names = {
                    item.partition("=")[0].lower()
                    for item in query.split("&")
                    if item
                }
                if names.intersection(_QUERY_SECRET_KEYS):
                    findings.append(
                        _finding("QUERY_SECRET", relative, node.lineno)
                    )
                if value is None:
                    continue
                try:
                    parsed_url = urlsplit(value)
                    parsed_port = parsed_url.port
                except ValueError:
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )
                    continue
                if parsed_url.scheme.lower() == "http":
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )
                if (
                    parsed_url.scheme.lower() in {"https", "wss"}
                    and call_name.lower() in _OUTBOUND_REQUEST_METHODS
                ):
                    hostname = parsed_url.hostname
                    default_port = 443
                    suffix = (
                        ""
                        if parsed_port in {None, default_port}
                        else f":{parsed_port}"
                    )
                    origin = (
                        f"{parsed_url.scheme.lower()}://{hostname}{suffix}"
                        if hostname is not None
                        else ""
                    )
                    exact_local_liveness = (
                        boundary_wrapper
                        and value == _LOCAL_LIVENESS_URL
                    )
                    if (
                        origin not in allowed_for_adapter
                        and not exact_local_liveness
                    ):
                        findings.append(
                            _finding(
                                "OUTBOUND_ORIGIN_UNAPPROVED",
                                relative,
                                node.lineno,
                            )
                        )

            if call_name == "OutboundPolicy":
                origin = _literal_string(
                    node.args[0] if node.args else None,
                    constants,
                )
                if (
                    origin not in _APPROVED_OUTBOUND_ORIGINS
                    or origin not in allowed_for_adapter
                ):
                    findings.append(
                        _finding(
                            "OUTBOUND_ORIGIN_UNAPPROVED",
                            relative,
                            node.lineno,
                        )
                    )

            for keyword in effective_keywords:
                keyword_name = (
                    keyword.arg.lower()
                    if keyword.arg is not None
                    else ""
                )
                if (
                    keyword_name
                    in {"base_url", "baseurl", "url_override"}
                    and call_name != "AlpacaExecutionTarget"
                ):
                    base_url = _literal_string(
                        keyword.value,
                        constants,
                    )
                    if base_url not in allowed_for_adapter:
                        findings.append(
                            _finding(
                                "OUTBOUND_ORIGIN_UNAPPROVED",
                                relative,
                                node.lineno,
                            )
                        )
                if (
                    keyword_name
                    in {"follow_redirects", "allow_redirects"}
                    and (network_option_call or boundary_wrapper)
                    and (
                        not isinstance(keyword.value, ast.Constant)
                        or keyword.value.value is not False
                    )
                ):
                    findings.append(
                        _finding(
                            "CROSS_ORIGIN_REDIRECT_ENABLED",
                            relative,
                            node.lineno,
                        )
                    )
                if (
                    keyword_name == "verify"
                    and (network_option_call or boundary_wrapper)
                    and (
                    not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value is not True
                    )
                    and not (
                    boundary_wrapper
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id in verified_tls_contexts
                    )
                ):
                    findings.append(
                        _finding("TLS_DISABLED", relative, node.lineno)
                    )
                if keyword_name in {
                    "trust_env",
                    "proxy_headers",
                } and (
                    not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value is not False
                ):
                    findings.append(
                        _finding(
                            "PROXY_HEADERS_TRUSTED",
                            relative,
                            node.lineno,
                        )
                    )
                if (
                    keyword_name == "forwarded_allow_ips"
                    and (
                    not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value != ""
                    )
                ):
                    findings.append(
                        _finding(
                            "PROXY_HEADERS_TRUSTED",
                            relative,
                            node.lineno,
                        )
                    )
                if (
                    keyword_name in {"proxy", "proxies"}
                    and (
                    not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value not in {None, False}
                    )
                    and not (
                    isinstance(keyword.value, ast.Dict)
                    and not keyword.value.keys
                    )
                ):
                    findings.append(
                        _finding(
                            "PROXY_HEADERS_TRUSTED",
                            relative,
                            node.lineno,
                        )
                    )
                if keyword_name in {
                    "ssl",
                    "tls",
                    "use_ssl",
                } and (
                    not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value is not True
                ) and not (
                    boundary_wrapper
                    and keyword_name == "ssl"
                    and isinstance(keyword.value, ast.Name)
                    and keyword.value.id in verified_tls_contexts
                ):
                    findings.append(
                        _finding("TLS_DISABLED", relative, node.lineno)
                    )
                if keyword_name in {
                    "ssl_certfile",
                    "ssl_keyfile",
                } and (
                    isinstance(keyword.value, ast.Constant)
                    and keyword.value.value in {None, False, ""}
                ):
                    findings.append(
                        _finding("TLS_DISABLED", relative, node.lineno)
                    )
                if (
                    keyword_name == "params"
                    and (network_option_call or boundary_wrapper)
                ):
                    approved_query_validation = (
                        boundary_wrapper
                        and isinstance(keyword.value, ast.Call)
                        and _attribute_path(keyword.value.func)
                        == ("_validated_query_params",)
                    )
                    keys, dynamic = (
                        mapping_aliases.get(keyword.value.id)
                        if isinstance(keyword.value, ast.Name)
                        and keyword.value.id in mapping_aliases
                        else _mapping_literal_keys(keyword.value)
                    )
                    if keys.intersection(_QUERY_SECRET_KEYS):
                        findings.append(
                            _finding(
                                "QUERY_SECRET",
                                relative,
                                node.lineno,
                            )
                        )
                    elif (
                        dynamic
                        and not approved_query_validation
                        and relative
                        not in {
                            "src/trading_assistant/backtest/coingecko.py",
                        }
                    ):
                        findings.append(
                            _finding(
                                "QUERY_SECRET",
                                relative,
                                node.lineno,
                            )
                        )
    return findings


def _tracked_artifact_code(name: str) -> str | None:
    path = PurePosixPath(name)
    basename = path.name.lower()
    lowered = name.lower()
    parent_names = {part.lower() for part in path.parts[:-1]}
    if basename in {".env", ".envrc"} or (
        basename.startswith(".env.")
        and basename
        not in {".env.example", ".env.sample", ".env.template"}
    ):
        return "TRACKED_ENV_FILE"
    if basename.endswith(
        (".wal", ".sqlite-wal", ".sqlite3-wal", ".db-wal")
    ):
        return "TRACKED_SQLITE_WAL"
    if basename.endswith(
        (".shm", ".sqlite-shm", ".sqlite3-shm", ".db-shm")
    ):
        return "TRACKED_SQLITE_SHM"
    if (
        basename in {"id_rsa", "id_ed25519"}
        or basename.endswith(".key")
        or basename.endswith("-key.pem")
        or "private-key" in basename
        or (
            basename.endswith((".pem", ".der"))
            and re.search(r"(?:^|[._-])private(?:[._-]|$)", basename)
            is not None
        )
        or (
            basename.endswith((".pem", ".der"))
            and re.search(r"(?:^|[._-])key(?:[._-]|$)", basename)
            is not None
        )
    ):
        return "TRACKED_TLS_PRIVATE_KEY"
    if basename.endswith((".p12", ".pfx", ".pkcs12")):
        return "TRACKED_TLS_PRIVATE_CERTIFICATE"
    encrypted_suffix = basename.endswith((".aesgcm", ".age", ".gpg"))
    backup_directory = bool(
        parent_names.intersection({"backup", "backups"})
    )
    backup_payload = basename.endswith(
        (
            ".bak",
            ".csv",
            ".db",
            ".dump",
            ".json",
            ".sql",
            ".sqlite",
            ".sqlite3",
            ".tar",
            ".txt",
            ".zip",
        )
    )
    plaintext_marker = (
        "decrypted" in lowered or "plaintext" in lowered
    )
    if (
        (
            backup_directory
            and backup_payload
            or plaintext_marker
            and backup_payload
            and not (
                path.suffix.lower() == ".md"
                and "docs" in parent_names
            )
        )
        and not encrypted_suffix
    ):
        return "TRACKED_DECRYPTED_BACKUP"
    if basename.endswith((".sqlite", ".sqlite3", ".db")):
        return "TRACKED_SQLITE_DATABASE"
    if basename.endswith(".log") or "/logs/" in f"/{lowered}":
        return "TRACKED_RUNTIME_LOG"
    if (
        "/exports/" in f"/{lowered}"
        or "raw-export" in basename
        or "raw_account_export" in basename
        or "raw-account-export" in basename
    ):
        return "TRACKED_RAW_EXPORT"
    return None


def _scan_tracked_artifacts(root: Path) -> list[ReleaseViolation]:
    tracked_names = _git_tracked_names(root)
    if tracked_names is None:
        return [_finding("GIT_TREE_UNPROVEN", ".", 1)]
    findings: list[ReleaseViolation] = []
    for name in tracked_names:
        code = _tracked_artifact_code(name)
        if code is not None:
            findings.append(_finding(code, name, 1))
    return findings


def _registered_environment_names(
    root: Path,
) -> tuple[frozenset[str], ReleaseViolation | None]:
    relative = "src/trading_assistant/security/secrets.py"
    path = root / relative
    if not path.exists():
        return (
            frozenset(),
            _finding("ENVIRONMENT_SECRETS_IN_PRODUCTION", relative, 1),
        )
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return (
            frozenset(),
            _finding("ENVIRONMENT_SECRETS_IN_PRODUCTION", relative, 1),
        )
    registry = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "_SIMPLE_SECRET_FIELDS"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ),
        None,
    )
    if registry is None or not isinstance(registry.value, (ast.Tuple, ast.List)):
        return (
            frozenset(),
            _finding("ENVIRONMENT_SECRETS_IN_PRODUCTION", relative, 1),
        )
    names: set[str] = set()
    for element in registry.value.elts:
        if not isinstance(element, ast.Constant) or not isinstance(
            element.value, str
        ):
            return (
                frozenset(),
                _finding(
                    "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                    relative,
                    getattr(element, "lineno", registry.lineno),
                ),
            )
        names.add(element.value.upper())
    names.add("FIELD_ENCRYPTION_KEYS_JSON")
    return frozenset(names), None


def _is_development_flag(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "args"
        and node.attr == "development_environment_secrets"
    )


def _is_os_environ(
    node: ast.AST,
    os_module_names: set[str] | frozenset[str] = frozenset({"os"}),
) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in os_module_names
        and node.attr == "environ"
    )


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _allowed_environment_branch_lines(
    tree: ast.AST,
    relative: str,
    provider_names: set[str],
    load_names: set[str],
    os_module_names: set[str],
) -> set[int]:
    expected_role = {
        "src/trading_assistant/db/migrate.py": "migration",
        "src/trading_assistant/ops/safety_drill.py": "safety-drill",
    }.get(relative)
    if expected_role is None:
        return set()
    allowed: set[int] = set()
    for branch in ast.walk(tree):
        if not isinstance(branch, ast.If) or not _is_development_flag(
            branch.test
        ):
            continue
        provider_calls: list[ast.Call] = []
        load_calls: list[ast.Call] = []
        provider_variables: set[str] = set()
        for statement in branch.body:
            for candidate in ast.walk(statement):
                if not isinstance(candidate, ast.Call):
                    continue
                name = _call_name(candidate)
                if name in provider_names:
                    provider_calls.append(candidate)
                if name in load_names:
                    load_calls.append(candidate)
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if isinstance(value, ast.Call) and _call_name(value) in provider_names:
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    provider_variables.update(
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    )
        valid_providers = {
            call.lineno
            for call in provider_calls
            if any(
                keyword.arg == "environ"
                and _is_os_environ(keyword.value, os_module_names)
                for keyword in call.keywords
            )
            and any(
                keyword.arg == "encryption"
                and isinstance(keyword.value, ast.Attribute)
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == "config"
                and keyword.value.attr == "encryption"
                for keyword in call.keywords
            )
        }
        if len(valid_providers) != len(provider_calls) or not provider_calls:
            continue
        valid_loads: list[ast.Call] = []
        for call in load_calls:
            role = _literal_string(call.args[0] if call.args else None)
            allow = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "allow_environment"
                ),
                None,
            )
            provider_arg = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "provider"
                ),
                None,
            )
            provider_proven = (
                isinstance(provider_arg, ast.Name)
                and provider_arg.id in provider_variables
            ) or (
                isinstance(provider_arg, ast.Call)
                and provider_arg.lineno in valid_providers
            )
            if (
                role == expected_role
                and isinstance(allow, ast.Constant)
                and allow.value is True
                and provider_proven
            ):
                valid_loads.append(call)
        if (
            not valid_loads
            or len(valid_loads) != len(load_calls)
        ):
            continue
        allowed.update(valid_providers)
        allowed.update(call.lineno for call in valid_loads)
        for provider_call in provider_calls:
            for candidate in ast.walk(provider_call):
                if hasattr(candidate, "lineno"):
                    allowed.add(candidate.lineno)
    return allowed


def _scan_environment_secret_sources(root: Path) -> list[ReleaseViolation]:
    registered, registry_finding = _registered_environment_names(root)
    findings = [registry_finding] if registry_finding is not None else []
    for path in _python_sources(root):
        relative = path.relative_to(root).as_posix()
        if relative == "src/trading_assistant/security/secrets.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue
        provider_names = {"EnvironmentSecretProvider"}
        load_names = {"load_role_secrets"}
        os_module_names = {"os"}
        getenv_names: set[str] = set()
        environ_names: set[str] = set()
        environment_mapping_copy_names = {"dict"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name == "os":
                        os_module_names.add(imported.asname or "os")
            elif isinstance(node, ast.ImportFrom):
                for imported in node.names:
                    local = imported.asname or imported.name
                    if imported.name == "EnvironmentSecretProvider":
                        provider_names.add(local)
                    elif imported.name == "load_role_secrets":
                        load_names.add(local)
                    elif node.module == "os" and imported.name == "getenv":
                        getenv_names.add(local)
                    elif node.module == "os" and imported.name == "environ":
                        environ_names.add(local)

        changed = True
        while changed:
            changed = False
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                names = {
                    target.id
                    for target in targets
                    if isinstance(target, ast.Name)
                }
                if not names:
                    continue
                value = node.value
                if isinstance(value, ast.Name) and value.id in provider_names:
                    before = len(provider_names)
                    provider_names.update(names)
                    changed = changed or len(provider_names) != before
                if (
                    isinstance(value, ast.Attribute)
                    and value.attr == "EnvironmentSecretProvider"
                ):
                    before = len(provider_names)
                    provider_names.update(names)
                    changed = changed or len(provider_names) != before
                if isinstance(value, ast.Name) and value.id in getenv_names:
                    before = len(getenv_names)
                    getenv_names.update(names)
                    changed = changed or len(getenv_names) != before
                if (
                    isinstance(value, ast.Name)
                    and value.id in environment_mapping_copy_names
                ):
                    before = len(environment_mapping_copy_names)
                    environment_mapping_copy_names.update(names)
                    changed = (
                        changed
                        or len(environment_mapping_copy_names) != before
                    )
                if isinstance(value, ast.Name) and value.id in environ_names:
                    before = len(environ_names)
                    environ_names.update(names)
                    changed = changed or len(environ_names) != before
                if _is_os_environ(value, os_module_names):
                    before = len(environ_names)
                    environ_names.update(names)
                    changed = changed or len(environ_names) != before
                if (
                    isinstance(value, ast.Attribute)
                    and isinstance(value.value, ast.Name)
                    and value.value.id in os_module_names
                    and value.attr == "getenv"
                ):
                    before = len(getenv_names)
                    getenv_names.update(names)
                    changed = changed or len(getenv_names) != before

        allowed_lines = _allowed_environment_branch_lines(
            tree,
            relative,
            provider_names,
            load_names,
            os_module_names,
        )
        provider_variables: set[str] = set()
        for assignment in ast.walk(tree):
            if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                continue
            if (
                isinstance(assignment.value, ast.Call)
                and _call_name(assignment.value) in provider_names
            ):
                provider_variables.update(
                    target.id
                    for target in _assignment_targets(assignment)
                    if isinstance(target, ast.Name)
                )
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in provider_variables
            ):
                continue
            parent = parents.get(node)
            grandparent = parents.get(parent) if parent is not None else None
            allowed_provider_argument = (
                isinstance(parent, ast.keyword)
                and parent.arg == "provider"
                and isinstance(grandparent, ast.Call)
                and _call_name(grandparent) in load_names
                and grandparent.lineno in allowed_lines
            )
            if not allowed_provider_argument:
                findings.append(
                    _finding(
                        "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                        relative,
                        node.lineno,
                    )
                )
        for node in ast.walk(tree):
            if not hasattr(node, "lineno") or node.lineno in allowed_lines:
                continue
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in provider_names:
                    findings.append(
                        _finding(
                            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                            relative,
                            node.lineno,
                        )
                    )
                    continue
                dynamic_import = name in {"import_module", "__import__"} and (
                    not node.args
                    or _literal_string(node.args[0]) is None
                )
                if dynamic_import:
                    findings.append(
                        _finding(
                            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                            relative,
                            node.lineno,
                        )
                    )
                    continue
                is_getenv = (
                    isinstance(node.func, ast.Name)
                    and node.func.id in getenv_names
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in os_module_names
                    and node.func.attr == "getenv"
                ) or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and (
                        _is_os_environ(node.func.value)
                        or _is_os_environ(
                            node.func.value,
                            os_module_names,
                        )
                        or isinstance(node.func.value, ast.Name)
                        and node.func.value.id in environ_names
                    )
                )
                environ_receiver = (
                    node.func.value
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                is_environ_mapping_call = (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr
                    in {
                        "__getitem__",
                        "copy",
                        "items",
                        "keys",
                        "values",
                    }
                    and (
                        _is_os_environ(
                            environ_receiver,
                            os_module_names,
                        )
                        or (
                            isinstance(environ_receiver, ast.Name)
                            and environ_receiver.id in environ_names
                        )
                    )
                )
                if is_environ_mapping_call:
                    findings.append(
                        _finding(
                            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                            relative,
                            node.lineno,
                        )
                    )
                copies_environment_mapping = (
                    isinstance(node.func, ast.Name)
                    and node.func.id in environment_mapping_copy_names
                    and any(
                        _is_os_environ(argument, os_module_names)
                        or isinstance(argument, ast.Name)
                        and argument.id in environ_names
                        for argument in node.args
                    )
                )
                if copies_environment_mapping:
                    findings.append(
                        _finding(
                            "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                            relative,
                            node.lineno,
                        )
                    )
                if is_getenv:
                    key = _literal_string(node.args[0] if node.args else None)
                    if key is None or key.upper() in registered:
                        findings.append(
                            _finding(
                                "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                                relative,
                                node.lineno,
                            )
                        )
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                ):
                    attribute = _literal_string(node.args[1])
                    receiver = node.args[0]
                    receiver_is_os = (
                        isinstance(receiver, ast.Name)
                        and receiver.id in os_module_names
                    )
                    if attribute == "EnvironmentSecretProvider" or (
                        attribute in {"environ", "getenv"}
                        and receiver_is_os
                    ):
                        findings.append(
                            _finding(
                                "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                                relative,
                                node.lineno,
                            )
                        )
                    elif attribute is None and receiver_is_os:
                        findings.append(
                            _finding(
                                "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                                relative,
                                node.lineno,
                            )
                        )
            elif isinstance(node, ast.Subscript):
                value = node.value
                is_environ = _is_os_environ(
                    value,
                    os_module_names,
                ) or (
                    isinstance(value, ast.Name)
                    and value.id in environ_names
                )
                if is_environ:
                    key = _literal_string(node.slice)
                    if key is None or key.upper() in registered:
                        findings.append(
                            _finding(
                                "ENVIRONMENT_SECRETS_IN_PRODUCTION",
                                relative,
                                node.lineno,
                            )
                        )
    return findings


def _root_sensitive_registry(
    root: Path,
) -> tuple[
    dict[str, frozenset[str]],
    ReleaseViolation | None,
]:
    relative = "src/trading_assistant/security/sensitive_fields.py"
    path = root / relative
    if not path.exists():
        return {}, _finding("SENSITIVE_REGISTRY_INVALID", relative, 1)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return {}, _finding("SENSITIVE_REGISTRY_INVALID", relative, 1)
    registry = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "SENSITIVE_FIELDS"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ),
        None,
    )
    if registry is None or not isinstance(registry.value, ast.Dict):
        return (
            {},
            _finding(
                "SENSITIVE_REGISTRY_INVALID",
                relative,
                getattr(registry, "lineno", 1),
            ),
        )
    parsed: dict[str, frozenset[str]] = {}
    for key, value in zip(registry.value.keys, registry.value.values, strict=True):
        if (
            not isinstance(key, ast.Constant)
            or not isinstance(key.value, str)
            or not key.value
            or not isinstance(value, (ast.Set, ast.Tuple, ast.List))
        ):
            return (
                {},
                _finding(
                    "SENSITIVE_REGISTRY_INVALID",
                    relative,
                    getattr(key, "lineno", registry.lineno),
                ),
            )
        fields: set[str] = set()
        for element in value.elts:
            if (
                not isinstance(element, ast.Constant)
                or not isinstance(element.value, str)
                or not element.value
            ):
                return (
                    {},
                    _finding(
                        "SENSITIVE_REGISTRY_INVALID",
                        relative,
                        getattr(element, "lineno", registry.lineno),
                    ),
                )
            fields.add(element.value)
        if not fields or key.value in parsed:
            return (
                {},
                _finding(
                    "SENSITIVE_REGISTRY_INVALID",
                    relative,
                    key.lineno,
                ),
            )
        parsed[key.value] = frozenset(fields)
    if not parsed:
        return (
            {},
            _finding(
                "SENSITIVE_REGISTRY_INVALID",
                relative,
                registry.lineno,
            ),
        )
    return parsed, None


def _root_model_fields(
    root: Path,
    table_fields: dict[str, frozenset[str]],
) -> tuple[
    dict[str, tuple[str, frozenset[str]]],
    ReleaseViolation | None,
]:
    relative = "src/trading_assistant/db/models.py"
    path = root / relative
    if not path.exists():
        if table_fields:
            return {}, _finding("SENSITIVE_REGISTRY_INVALID", relative, 1)
        return {}, None
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return {}, _finding("SENSITIVE_REGISTRY_INVALID", relative, 1)
    models: dict[str, tuple[str, frozenset[str]]] = {}
    seen_tables: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        table_name: str | None = None
        for statement in node.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if not any(
                isinstance(target, ast.Name)
                and target.id == "__tablename__"
                for target in targets
            ):
                continue
            table_name = _literal_string(statement.value)
        if table_name in table_fields:
            models[node.name] = (table_name, table_fields[table_name])
            seen_tables.add(table_name)
    if set(table_fields) != seen_tables:
        return {}, _finding("SENSITIVE_REGISTRY_INVALID", relative, 1)
    return models, None


def _scan_root_sensitive_writes(root: Path) -> list[ReleaseViolation]:
    table_fields, registry_finding = _root_sensitive_registry(root)
    if registry_finding is not None:
        return [registry_finding]
    if not table_fields:
        return []
    model_fields, model_finding = _root_model_fields(root, table_fields)
    if model_finding is not None:
        return [model_finding]
    scanner_path = (
        DEFAULT_ROOT
        / "src"
        / "trading_assistant"
        / "security"
        / "sensitive_write_scan.py"
    )
    try:
        spec = importlib.util.spec_from_file_location(
            "_release_sensitive_write_scan",
            scanner_path,
        )
        if spec is None or spec.loader is None:
            raise ImportError
        scanner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scanner_module)
        scan_sensitive_writes = scanner_module.scan_sensitive_writes
    except Exception:
        return [
            _finding(
                "SENSITIVE_REGISTRY_INVALID",
                "src/trading_assistant/security/sensitive_fields.py",
                1,
            )
        ]
    source_root = root / "src" / "trading_assistant"
    allowed = {
        source_root / "security" / "sensitive_fields.py",
        source_root / "ops" / "encrypt_sensitive.py",
    }
    findings: list[ReleaseViolation] = []
    for path in sorted(source_root.rglob("*.py")):
        if path in allowed:
            continue
        try:
            offenders = scan_sensitive_writes(
                [path],
                model_fields=model_fields,
                table_fields=table_fields,
            )
        except (OSError, SyntaxError, TypeError, ValueError):
            findings.append(
                _finding(
                    "SENSITIVE_REGISTRY_INVALID",
                    path.relative_to(root).as_posix(),
                    1,
                )
            )
            continue
        if not offenders:
            continue
        lines: set[int] = set()
        for offender in offenders:
            pieces = offender.rsplit(":", 2)
            try:
                line = int(pieces[-2])
            except (IndexError, ValueError):
                line = 1
            lines.add(max(1, line))
        relative = path.relative_to(root).as_posix()
        findings.extend(
            _finding("PLAINTEXT_SENSITIVE_WRITE", relative, line)
            for line in sorted(lines)
        )
    return findings


_CHAT_TOOL_ALLOWLIST = frozenset(
    {
        "get_market_data",
        "get_account_summary",
        "get_open_orders",
        "get_order_status",
        "list_rules",
        "draft_order_candidate",
        "draft_rule_candidate",
    }
)
_CHAT_READ_METHODS = frozenset(
    {
        "get_market_data",
        "get_account_summary",
        "get_open_orders",
        "get_order_status",
        "list_rules",
    }
)
_CHAT_DRAFT_METHODS = frozenset({"draft_order", "draft_rule"})
_MUTATION_TOOL_TOKENS = frozenset(
    {
        "add",
        "approve",
        "cancel",
        "commit",
        "create",
        "delete",
        "execute",
        "flush",
        "notify",
        "panic",
        "persist",
        "queue",
        "reconcile",
        "reject",
        "release",
        "reset",
        "send",
        "submit",
        "sync",
        "trip",
        "update",
        "write",
    }
)


def _mutable_tool_name(name: str) -> bool:
    parts = {
        part
        for part in re.split(r"[^a-z0-9]+", name.lower())
        if part
    }
    return bool(parts.intersection(_MUTATION_TOOL_TOKENS))


def _class_method(
    tree: ast.Module,
    class_name: str,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    cls = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == class_name
        ),
        None,
    )
    if cls is None:
        return None
    return next(
        (
            node
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == method_name
        ),
        None,
    )


def _attribute_path(node: ast.AST | None) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


def _scan_chat_tool_boundary(root: Path) -> list[ReleaseViolation]:
    relative = "src/trading_assistant/app/agent.py"
    path = root / relative
    if not path.exists():
        return [_finding("CHAT_TOOL_REGISTRY_UNPROVEN", relative, 1)]
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, SyntaxError):
        return [_finding("CHAT_TOOL_REGISTRY_UNPROVEN", relative, 1)]
    findings: list[ReleaseViolation] = []
    tool_router_methods = {
        node.name: node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        and cls.name == "ToolRouter"
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    registry = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name)
                and target.id == "READ_ONLY_TOOL_SPECS"
                for target in (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
            )
        ),
        None,
    )
    spec_names: list[str] = []
    if registry is None or not isinstance(registry.value, ast.Tuple):
        findings.append(
            _finding(
                "CHAT_TOOL_REGISTRY_UNPROVEN",
                relative,
                getattr(registry, "lineno", 1),
            )
        )
    else:
        for element in registry.value.elts:
            if not isinstance(element, ast.Dict):
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        getattr(element, "lineno", registry.lineno),
                    )
                )
                continue
            name: str | None = None
            for key, value in zip(element.keys, element.values, strict=True):
                if _literal_string(key) == "name":
                    name = _literal_string(value)
            if name is None:
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        element.lineno,
                    )
                )
            else:
                spec_names.append(name)
                if name not in _CHAT_TOOL_ALLOWLIST or _mutable_tool_name(name):
                    findings.append(
                        _finding("MUTABLE_CHAT_TOOL", relative, element.lineno)
                    )
        if (
            frozenset(spec_names) != _CHAT_TOOL_ALLOWLIST
            or len(spec_names) != len(_CHAT_TOOL_ALLOWLIST)
        ):
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    registry.lineno,
                )
            )

    dispatch = _class_method(tree, "ToolRouter", "dispatch")
    draft = _class_method(tree, "ToolRouter", "_draft")
    if dispatch is None or draft is None:
        findings.append(
            _finding("CHAT_TOOL_REGISTRY_UNPROVEN", relative, 1)
        )
    else:
        table_assignment = next(
            (
                node
                for node in ast.walk(dispatch)
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name)
                    and target.id == "table"
                    for target in (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else [node.target]
                    )
                )
            ),
            None,
        )
        dispatch_names: list[str] = []
        if table_assignment is None or not isinstance(
            table_assignment.value, ast.Dict
        ):
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    getattr(table_assignment, "lineno", dispatch.lineno),
                )
            )
        else:
            alias_methods: dict[str, str] = {}
            for node in ast.walk(dispatch):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                if not isinstance(value, ast.Attribute):
                    continue
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        alias_methods[target.id] = value.attr
            for key, value in zip(
                table_assignment.value.keys,
                table_assignment.value.values,
                strict=True,
            ):
                name = _literal_string(key)
                if name is None:
                    findings.append(
                        _finding(
                            "CHAT_TOOL_REGISTRY_UNPROVEN",
                            relative,
                            getattr(key, "lineno", table_assignment.lineno),
                        )
                    )
                    continue
                dispatch_names.append(name)
                if name not in _CHAT_TOOL_ALLOWLIST:
                    findings.append(
                        _finding(
                            "MUTABLE_CHAT_TOOL",
                            relative,
                            getattr(key, "lineno", table_assignment.lineno),
                        )
                    )
                if not isinstance(value, ast.Lambda):
                    findings.append(
                        _finding(
                            "CHAT_TOOL_REGISTRY_UNPROVEN",
                            relative,
                            getattr(value, "lineno", table_assignment.lineno),
                        )
                    )
                    continue
                for call in (
                    node
                    for node in ast.walk(value.body)
                    if isinstance(node, ast.Call)
                ):
                    if (
                        isinstance(call.func, ast.Name)
                        and call.func.id == "getattr"
                    ):
                        findings.append(
                            _finding(
                                "CHAT_TOOL_REGISTRY_UNPROVEN",
                                relative,
                                call.lineno,
                            )
                        )
                        continue
                    called = (
                        call.func.attr
                        if isinstance(call.func, ast.Attribute)
                        else alias_methods.get(call.func.id)
                        if isinstance(call.func, ast.Name)
                        else None
                    )
                    call_path = (
                        _attribute_path(call.func)
                        if isinstance(call.func, ast.Attribute)
                        else ()
                    )
                    local_helper = (
                        called is not None
                        and call_path == ("self", called)
                        and called in tool_router_methods
                    )
                    if called is None:
                        findings.append(
                            _finding(
                                "CHAT_TOOL_REGISTRY_UNPROVEN",
                                relative,
                                call.lineno,
                            )
                        )
                    elif (
                        called not in _CHAT_READ_METHODS
                        and called not in _CHAT_DRAFT_METHODS
                        and called != "_draft"
                        and not local_helper
                    ):
                        findings.append(
                            _finding(
                                "MUTABLE_CHAT_TOOL"
                                if _mutable_tool_name(called)
                                else "CHAT_TOOL_REGISTRY_UNPROVEN",
                                relative,
                                call.lineno,
                            )
                        )
                    elif not local_helper and (
                        name in _CHAT_READ_METHODS
                        and call_path != ("s", name)
                    ) or (
                        name
                        in {
                            "draft_order_candidate",
                            "draft_rule_candidate",
                        }
                        and call_path != ("self", "_draft")
                    ):
                        findings.append(
                            _finding(
                                "CHAT_TOOL_REGISTRY_UNPROVEN",
                                relative,
                                call.lineno,
                            )
                        )
            if (
                frozenset(dispatch_names) != _CHAT_TOOL_ALLOWLIST
                or len(dispatch_names) != len(_CHAT_TOOL_ALLOWLIST)
            ):
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        table_assignment.lineno,
                    )
                )

        for function in (dispatch, draft):
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "__import__"}
                ):
                    findings.append(
                        _finding(
                            "CHAT_TOOL_REGISTRY_UNPROVEN",
                            relative,
                            node.lineno,
                        )
                    )
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    findings.append(
                        _finding(
                            "CHAT_TOOL_REGISTRY_UNPROVEN",
                            relative,
                            node.lineno,
                        )
                    )
        approved_draft_constructors: set[str] = set()
        for call in (
            node
            for node in ast.walk(draft)
            if isinstance(node, ast.Call)
        ):
            called = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else ""
            )
            call_path = (
                _attribute_path(call.func)
                if isinstance(call.func, ast.Attribute)
                else ()
            )
            if call_path in {
                ("self", "candidate_drafts", "draft_order"),
                ("self", "candidate_drafts", "draft_rule"),
            }:
                approved_draft_constructors.add(call_path[-1])
                continue
            if call_path == ("envelope", "model_dump") or (
                isinstance(call.func, ast.Name)
                and call.func.id == "CandidateError"
            ):
                continue
            findings.append(
                _finding(
                    "MUTABLE_CHAT_TOOL"
                    if called and _mutable_tool_name(called)
                    else "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    call.lineno,
                )
            )
        if approved_draft_constructors != _CHAT_DRAFT_METHODS:
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    draft.lineno,
                )
            )
        for assignment in (
            node
            for node in ast.walk(draft)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        ):
            targets = (
                assignment.targets
                if isinstance(assignment, ast.Assign)
                else [assignment.target]
            )
            if any(
                isinstance(target, (ast.Attribute, ast.Subscript))
                for target in targets
            ):
                findings.append(
                    _finding(
                        "MUTABLE_CHAT_TOOL",
                        relative,
                        assignment.lineno,
                )
            )

        helper_entrypoints: set[str] = set()
        if table_assignment is not None and isinstance(
            table_assignment.value,
            ast.Dict,
        ):
            for value in table_assignment.value.values:
                if not isinstance(value, ast.Lambda):
                    continue
                for call in ast.walk(value.body):
                    if not isinstance(call, ast.Call):
                        continue
                    path_parts = _attribute_path(call.func)
                    if (
                        len(path_parts) == 2
                        and path_parts[0] == "self"
                        and path_parts[1] in tool_router_methods
                        and path_parts[1] != "_draft"
                    ):
                        helper_entrypoints.add(path_parts[1])

        checked_helpers: set[str] = set()
        active_helpers: set[str] = set()

        def inspect_helper(name: str) -> None:
            if name in checked_helpers:
                return
            if name in active_helpers:
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        tool_router_methods[name].lineno,
                    )
                )
                return
            helper = tool_router_methods.get(name)
            if helper is None:
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        dispatch.lineno,
                    )
                )
                return
            active_helpers.add(name)
            for mutation in (
                node
                for node in ast.walk(helper)
                if isinstance(
                    node,
                    (
                        ast.Assign,
                        ast.AnnAssign,
                        ast.AugAssign,
                        ast.Delete,
                        ast.NamedExpr,
                    ),
                )
            ):
                targets = (
                    mutation.targets
                    if isinstance(mutation, (ast.Assign, ast.Delete))
                    else [mutation.target]
                )
                state_mutation = any(
                    isinstance(target, (ast.Attribute, ast.Subscript))
                    for target in targets
                ) or isinstance(
                    mutation,
                    (ast.AugAssign, ast.Delete),
                )
                findings.append(
                    _finding(
                        "MUTABLE_CHAT_TOOL"
                        if state_mutation
                        else "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        mutation.lineno,
                    )
                )
            if any(
                isinstance(node, (ast.Lambda, ast.Import, ast.ImportFrom))
                for node in ast.walk(helper)
            ):
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        helper.lineno,
                    )
                )
            for call in (
                node
                for node in ast.walk(helper)
                if isinstance(node, ast.Call)
            ):
                if (
                    isinstance(call.func, ast.Name)
                    and call.func.id in {"getattr", "__import__"}
                ) or (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "import_module"
                ):
                    findings.append(
                        _finding(
                            "CHAT_TOOL_REGISTRY_UNPROVEN",
                            relative,
                            call.lineno,
                        )
                    )
                    continue
                path_parts = _attribute_path(call.func)
                called = (
                    path_parts[-1]
                    if path_parts
                    else call.func.id
                    if isinstance(call.func, ast.Name)
                    else None
                )
                if (
                    len(path_parts) == 2
                    and path_parts[0] == "self"
                    and called in tool_router_methods
                ):
                    inspect_helper(called)
                    continue
                if (
                    len(path_parts) == 2
                    and path_parts[0] != "self"
                    and called in _CHAT_READ_METHODS
                ):
                    continue
                if called is not None and _mutable_tool_name(called):
                    findings.append(
                        _finding(
                            "MUTABLE_CHAT_TOOL",
                            relative,
                            call.lineno,
                        )
                    )
                    continue
                if (
                    isinstance(call.func, ast.Name)
                    and call.func.id
                    in {"bool", "dict", "int", "len", "list", "str", "tuple"}
                ):
                    continue
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        call.lineno,
                    )
                )
            active_helpers.remove(name)
            checked_helpers.add(name)

        for helper_name in sorted(helper_entrypoints):
            inspect_helper(helper_name)

    chat = _class_method(tree, "Agent", "chat")
    if chat is None:
        findings.append(
            _finding("CHAT_TOOL_REGISTRY_UNPROVEN", relative, 1)
        )
    else:
        backend_calls = [
            node
            for node in ast.walk(chat)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create"
        ]
        if not backend_calls:
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    chat.lineno,
                )
            )
        for call in backend_calls:
            tools = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "tools"
                ),
                None,
            )
            if not (
                isinstance(tools, ast.Name)
                and tools.id == "READ_ONLY_TOOL_SPECS"
            ):
                findings.append(
                    _finding(
                        "CHAT_TOOL_REGISTRY_UNPROVEN",
                        relative,
                        call.lineno,
                    )
                )
        if not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "dispatch"
            for node in ast.walk(chat)
        ):
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    chat.lineno,
                )
            )

    reachable = [
        function
        for function in (dispatch, draft, chat)
        if function is not None
    ]
    for function in reachable:
        aliases: dict[str, str] = {}
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if not isinstance(node.value, ast.Attribute):
                continue
            targets = (
                node.targets
                if isinstance(node, ast.Assign)
                else [node.target]
            )
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = node.value.attr
        for call in (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
        ):
            called = (
                call.func.attr
                if isinstance(call.func, ast.Attribute)
                else aliases.get(call.func.id)
                if isinstance(call.func, ast.Name)
                else None
            )
            if called is None or not _mutable_tool_name(called):
                continue
            path_parts = (
                _attribute_path(call.func)
                if isinstance(call.func, ast.Attribute)
                else ()
            )
            approved_backend_create = (
                called == "create"
                and path_parts == ("self", "backend", "create")
            )
            approved_draft = (
                function is draft
                and called in _CHAT_DRAFT_METHODS
            )
            if not approved_backend_create and not approved_draft:
                findings.append(
                    _finding("MUTABLE_CHAT_TOOL", relative, call.lineno)
                )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
        ) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
        ):
            findings.append(
                _finding(
                    "CHAT_TOOL_REGISTRY_UNPROVEN",
                    relative,
                    node.lineno,
                )
            )
    return findings


_LEGACY_RELEASE_CHECKS = (
    (
        _check_config,
        "CONFIG_DEFAULT_UNSAFE",
        "config.yaml",
    ),
    (
        _check_no_runtime_create_all,
        "RUNTIME_SCHEMA_MUTATION",
        "src/trading_assistant",
    ),
    (
        _check_submission_paths,
        "BROKER_SUBMISSION_PATH_UNAPPROVED",
        "src/trading_assistant",
    ),
    (
        _check_no_raw_broker_escape,
        "RAW_BROKER_ESCAPE",
        "src/trading_assistant",
    ),
    (
        _check_browser_sources,
        "UNSAFE_BROWSER_SOURCE",
        "src/trading_assistant/app/static",
    ),
    (
        _check_no_unofficial_robinhood_dependency,
        "UNOFFICIAL_BROKER_DEPENDENCY",
        "pyproject.toml",
    ),
    (
        _check_llm_escape_paths,
        "LLM_CONSTRUCTION_UNPROVEN",
        "src/trading_assistant/llm",
    ),
    (
        _check_llm_construction_paths,
        "LLM_CONSTRUCTION_UNPROVEN",
        "src/trading_assistant/llm",
    ),
    (
        _check_no_deleted_rate_limiter_import,
        "DELETED_RATE_LIMITER_REFERENCE",
        "src/trading_assistant/app",
    ),
    (
        _check_encrypted_operational_backup_surface,
        "PLAINTEXT_BACKUP_SURFACE",
        "src/trading_assistant/ops/backup.py",
    ),
)

_STRUCTURED_RELEASE_SCANNERS = (
    (
        _scan_canonical_authorities,
        "INTERNAL_GATE_ERROR",
        "internal",
    ),
    (
        _scan_effective_route_graph,
        "ROUTE_REGISTRATION_UNPROVEN",
        "src/trading_assistant/app/main.py",
    ),
    (
        _scan_chat_tool_boundary,
        "CHAT_TOOL_REGISTRY_UNPROVEN",
        "src/trading_assistant/app/agent.py",
    ),
    (
        _scan_root_sensitive_writes,
        "SENSITIVE_REGISTRY_INVALID",
        "src/trading_assistant/security/sensitive_fields.py",
    ),
    (
        _scan_environment_secret_sources,
        "ENVIRONMENT_SECRETS_IN_PRODUCTION",
        "src/trading_assistant/security/secrets.py",
    ),
    (
        _scan_outbound_clients,
        "OUTBOUND_CLIENT_UNAPPROVED",
        "src/trading_assistant/security/outbound.py",
    ),
    (
        _scan_transport_and_integrations,
        "CONFIG_UNPROVEN",
        "config.yaml",
    ),
    (
        _scan_tracked_artifacts,
        "GIT_TREE_UNPROVEN",
        ".",
    ),
)


def _collect_release_violations(root: Path) -> list[ReleaseViolation]:
    hermetic_findings = _scan_hermetic_root(root)
    if hermetic_findings:
        return sorted(set(hermetic_findings))
    findings: list[ReleaseViolation] = []
    for check, code, relative in _LEGACY_RELEASE_CHECKS:
        try:
            check(root)
        except Exception:
            findings.append(_finding(code, relative, 1))
    for scan, code, relative in _STRUCTURED_RELEASE_SCANNERS:
        try:
            findings.extend(scan(root))
        except Exception:
            findings.append(_finding(code, relative, 1))
    return sorted(set(findings))


class _ValueFreeArgumentParser(argparse.ArgumentParser):
    """Reject malformed CLI input without reflecting it to stderr."""

    def error(self, _message: str) -> None:
        raise ValueError("invalid release-gate arguments")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _ValueFreeArgumentParser(description=__doc__)
        parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
        args = parser.parse_args(argv)
        root = args.root.resolve(strict=True)
        findings = _collect_release_violations(root)
    except SystemExit as exc:
        if exc.code == 0:
            return 0
        findings = [_finding("INTERNAL_GATE_ERROR", "internal", 1)]
    except BaseException:
        findings = [_finding("INTERNAL_GATE_ERROR", "internal", 1)]
    if findings:
        for finding in findings:
            print(
                f"{finding.code} {finding.path}:{finding.line}",
                file=sys.stderr,
            )
        count = len(findings)
        noun = "violation" if count == 1 else "violations"
        print(
            f"release static checks: FAIL ({count} {noun})",
            file=sys.stderr,
        )
        return 1
    print("release static checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
