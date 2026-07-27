#!/usr/bin/env python3
"""Fail closed when release-critical source defaults or paths drift."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

import yaml


DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def _fail(message: str) -> None:
    raise RuntimeError(message)


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
_NON_ROUTE_DECORATORS = {"field_validator", "model_validator", "wraps"}
_LLM_BACKEND_CLASSES = {
    "AnthropicBackend",
    "GeminiBackend",
    "GroqBackend",
}
_LLM_BACKEND_ALLOWED_PATHS = {
    "src/trading_assistant/llm/factory.py",
    "src/trading_assistant/llm/anthropic_backend.py",
    "src/trading_assistant/llm/gemini_backend.py",
    "src/trading_assistant/llm/groq_backend.py",
}


def _canonical_route_path(
    path: str,
    *,
    kind: str,
    relative: str,
    lineno: int,
) -> str:
    if (
        not path.startswith("/")
        or "//" in path
        or (path != "/" and path.endswith("/"))
    ):
        _fail(f"noncanonical {kind} path: {relative}:{lineno}")
    return path


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
        path = _canonical_route_path(
            entry.args[1].value,
            kind="route policy",
            relative=relative,
            lineno=entry.lineno,
        )
        policies.add((entry.args[0].value.upper(), path))
    return policies


def _route_decorator_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
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
            decorator = (
                value.attr
                if isinstance(value, ast.Attribute)
                else aliases.get(value.id)
                if isinstance(value, ast.Name)
                else None
            )
            if decorator not in (
                set(_ROUTE_DECORATOR_METHODS)
                | _GENERIC_ROUTE_DECORATORS
            ):
                continue
            for name in names:
                if aliases.get(name) != decorator:
                    aliases[name] = decorator
                    changed = True
    return aliases


def _decorated_routes(path: Path, root: Path) -> list[tuple[str, str]]:
    relative = path.relative_to(root).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    aliases = _route_decorator_aliases(tree)
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
            route_path = _canonical_route_path(
                path_arg.value,
                kind="route decorator",
                relative=relative,
                lineno=node.lineno,
            )
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


def _check_route_policy_inventory(root: Path) -> None:
    app = root / "src" / "trading_assistant" / "app"
    route_files = [app / "main.py", *sorted((app / "routers").glob("*.py"))]
    policies = _literal_route_policies(root)
    missing = sorted(
        {
            route
            for path in route_files
            if path.exists()
            for route in _decorated_routes(path, root)
            if route not in policies
        }
    )
    if missing:
        method, path = missing[0]
        _fail(f"route missing from ROUTE_POLICIES: {method} {path}")


def _check_llm_construction_paths(root: Path) -> None:
    runtime = root / "src" / "trading_assistant"
    offenders: list[str] = []
    for path in sorted(runtime.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative in _LLM_BACKEND_ALLOWED_PATHS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
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
            )
            if imported_backend or raw_reference:
                offenders.append(f"{relative}:{node.lineno}")
    if offenders:
        _fail(
            "raw LLM backend reference outside factory: "
            + ", ".join(sorted(set(offenders)))
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


def _check_no_tracked_secret_files(root: Path) -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=root,
    ).decode().split("\0")
    secret_names = {".env", ".env.local", "id_rsa", "id_ed25519"}
    offenders = [
        name
        for name in tracked
        if name and (Path(name).name in secret_names or Path(name).suffix in {".pem", ".p12"})
    ]
    if offenders:
        _fail("tracked secret-bearing file: " + ", ".join(offenders))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    checks = (
        _check_config,
        _check_no_runtime_create_all,
        _check_submission_paths,
        _check_browser_sources,
        _check_no_unofficial_robinhood_dependency,
        _check_route_policy_inventory,
        _check_llm_construction_paths,
        _check_no_deleted_rate_limiter_import,
        _check_no_tracked_secret_files,
    )
    try:
        for check in checks:
            check(root)
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"release static checks: FAIL ({exc})", file=sys.stderr)
        return 1
    print("release static checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
