"""Conservative static gate for bypasses of ``SensitiveFieldStore``.

The runtime session guards are authoritative.  This scanner is an earlier,
actionable release check: when it cannot prove a mapped write excludes
registered sensitive columns, it reports the write site.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
import re

from ..db.models import Base
from .sensitive_fields import SENSITIVE_FIELDS


def _registered_models() -> dict[str, tuple[str, frozenset[str]]]:
    registered: dict[str, tuple[str, frozenset[str]]] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table.name
        if table in SENSITIVE_FIELDS:
            registered[mapper.class_.__name__] = (
                table,
                frozenset(SENSITIVE_FIELDS[table]),
            )
    return registered


MODEL_FIELDS = _registered_models()
TABLE_FIELDS = {
    table: frozenset(fields)
    for table, fields in SENSITIVE_FIELDS.items()
}
UNIQUE_FIELDS = (
    set().union(*(fields for _, fields in MODEL_FIELDS.values()))
    - {"reason"}
)
_DML_PREFIX = re.compile(r"^\s*(?:insert|update|replace)\b", re.I)


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _annotation_name(node: ast.AST | None) -> str | None:
    direct = _name(node)
    if direct is not None:
        return direct
    if isinstance(node, ast.Subscript):
        return _annotation_name(node.slice)
    if isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            candidate = _annotation_name(element)
            if candidate is not None:
                return candidate
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_name(node.left) or _annotation_name(node.right)
    return None


class _SensitiveWriteVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.offenders: list[str] = []
        self.class_aliases = {name: name for name in MODEL_FIELDS}
        self.object_models: dict[str, str] = {}
        self.statement_models: dict[str, str] = {}
        self.string_constants: dict[str, str] = {}

    def _report(
        self,
        node: ast.AST,
        owner: str,
        field: str,
    ) -> None:
        item = f"{self.path}:{node.lineno}:{owner}.{field}"
        if item not in self.offenders:
            self.offenders.append(item)

    def _model(self, node: ast.AST | None) -> str | None:
        candidate = _name(node)
        if candidate is None:
            return None
        return self.class_aliases.get(candidate)

    def _object_model(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return self.object_models.get(node.id)
        return None

    def _call_model(self, node: ast.AST | None) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        direct = self._model(node.func)
        if direct is not None:
            return direct
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "merge"}
            and node.args
        ):
            return self._model(node.args[0])
        return None

    def _mutation_model(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return self.statement_models.get(node.id)
        if not isinstance(node, ast.Call):
            return None
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {
                "values",
                "returning",
                "where",
                "execution_options",
                "on_conflict_do_update",
            }
        ):
            return self._mutation_model(node.func.value)
        if _name(node.func) in {"insert", "update"} and node.args:
            return self._model(node.args[0])
        return None

    def _mapping_fields(
        self,
        node: ast.AST,
    ) -> tuple[set[str], bool]:
        fields: set[str] = set()
        dynamic = False
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is None:
                    dynamic = True
                elif isinstance(key, ast.Constant) and isinstance(
                    key.value, str
                ):
                    fields.add(key.value)
                else:
                    dynamic = True
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                nested_fields, nested_dynamic = self._mapping_fields(element)
                fields.update(nested_fields)
                dynamic = dynamic or nested_dynamic
        elif isinstance(node, ast.Starred):
            dynamic = True
        return fields, dynamic

    def _check_mapping(
        self,
        node: ast.AST,
        model: str,
        *,
        args: Iterable[ast.AST] = (),
        keywords: Iterable[ast.keyword] = (),
    ) -> None:
        sensitive = MODEL_FIELDS[model][1]
        fields: set[str] = set()
        dynamic = False
        for argument in args:
            argument_fields, argument_dynamic = self._mapping_fields(
                argument
            )
            fields.update(argument_fields)
            dynamic = dynamic or argument_dynamic
            if not isinstance(argument, (ast.Dict, ast.List, ast.Tuple)):
                dynamic = True
        for keyword in keywords:
            if keyword.arg is None:
                dynamic = True
                unpacked_fields, unpacked_dynamic = self._mapping_fields(
                    keyword.value
                )
                fields.update(unpacked_fields)
                dynamic = dynamic or unpacked_dynamic
            else:
                fields.add(keyword.arg)
        for field in sorted(sensitive.intersection(fields)):
            self._report(node, model, field)
        if dynamic:
            self._report(node, model, "**mapping")

    def _constant_string(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.string_constants.get(node.id)
        if (
            isinstance(node, ast.Call)
            and _name(node.func) == "text"
            and node.args
        ):
            return self._constant_string(node.args[0])
        return None

    def _check_raw_sql(self, node: ast.AST, expression: ast.AST) -> None:
        sql = self._constant_string(expression)
        if sql is None or _DML_PREFIX.match(sql) is None:
            return
        lowered = sql.lower()
        for table, fields in TABLE_FIELDS.items():
            if re.search(rf"\b{re.escape(table.lower())}\b", lowered) is None:
                continue
            for field in sorted(fields):
                if re.search(
                    rf"\b{re.escape(field.lower())}\b",
                    lowered,
                ):
                    self._report(node, table, field)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name in MODEL_FIELDS:
                self.class_aliases[
                    imported.asname or imported.name
                ] = imported.name
        self.generic_visit(node)

    def _record_target(
        self,
        target: ast.AST,
        value: ast.AST,
        annotation: ast.AST | None = None,
    ) -> None:
        if not isinstance(target, ast.Name):
            return
        model_alias = self._model(value)
        if model_alias is not None:
            self.class_aliases[target.id] = model_alias
        object_model = self._call_model(value)
        annotated = self._model_name_from_annotation(annotation)
        if object_model is not None or annotated is not None:
            self.object_models[target.id] = object_model or annotated  # type: ignore[assignment]
        statement_model = self._mutation_model(value)
        if statement_model is not None:
            self.statement_models[target.id] = statement_model
        constant = self._constant_string(value)
        if constant is not None:
            self.string_constants[target.id] = constant

    def _model_name_from_annotation(
        self,
        annotation: ast.AST | None,
    ) -> str | None:
        candidate = _annotation_name(annotation)
        if candidate is None:
            return None
        return self.class_aliases.get(candidate)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                model = self._object_model(target.value)
                if model is not None and target.attr in MODEL_FIELDS[model][1]:
                    self._report(target, model, target.attr)
                elif target.attr in UNIQUE_FIELDS:
                    self._report(target, "*", target.attr)
            self._record_target(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Attribute):
            model = self._object_model(node.target.value)
            if (
                model is not None
                and node.target.attr in MODEL_FIELDS[model][1]
            ):
                self._report(node.target, model, node.target.attr)
            elif node.target.attr in UNIQUE_FIELDS:
                self._report(node.target, "*", node.target.attr)
        if node.value is not None:
            self._record_target(node.target, node.value, node.annotation)
        elif isinstance(node.target, ast.Name):
            model = self._model_name_from_annotation(node.annotation)
            if model is not None:
                self.object_models[node.target.id] = model
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if isinstance(node.target, ast.Attribute):
            model = self._object_model(node.target.value)
            if (
                model is not None
                and node.target.attr in MODEL_FIELDS[model][1]
            ):
                self._report(node.target, model, node.target.attr)
            elif node.target.attr in UNIQUE_FIELDS:
                self._report(node.target, "*", node.target.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prior_objects = dict(self.object_models)
        for argument in (
            list(node.args.posonlyargs)
            + list(node.args.args)
            + list(node.args.kwonlyargs)
        ):
            model = self._model_name_from_annotation(argument.annotation)
            if model is not None:
                self.object_models[argument.arg] = model
        self.generic_visit(node)
        self.object_models = prior_objects

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        model = self._model(node.func)
        if model is not None:
            self._check_mapping(
                node,
                model,
                keywords=node.keywords,
            )

        mutation_model = self._mutation_model(node)
        if (
            mutation_model is not None
            and isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"values", "on_conflict_do_update"}
        ):
            self._check_mapping(
                node,
                mutation_model,
                args=node.args,
                keywords=node.keywords,
            )

        call_name = _name(node.func)
        if (
            isinstance(node.func, ast.Attribute)
            and call_name
            in {"bulk_insert_mappings", "bulk_update_mappings"}
            and node.args
        ):
            bulk_model = self._model(node.args[0])
            if bulk_model is not None:
                self._check_mapping(
                    node,
                    bulk_model,
                    args=node.args[1:],
                    keywords=node.keywords,
                )

        if call_name == "setattr" and len(node.args) >= 2:
            object_model = self._object_model(node.args[0])
            field = (
                node.args[1].value
                if isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
                else None
            )
            if (
                object_model is not None
                and field in MODEL_FIELDS[object_model][1]
            ):
                self._report(node, object_model, field)
            elif field in UNIQUE_FIELDS:
                self._report(node, "*", field)

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            execute_model = self._mutation_model(node.args[0])
            if execute_model is not None and len(node.args) > 1:
                self._check_mapping(
                    node,
                    execute_model,
                    args=node.args[1:],
                    keywords=node.keywords,
                )
            self._check_raw_sql(node, node.args[0])

        self.generic_visit(node)


def scan_sensitive_writes(paths: Iterable[Path]) -> list[str]:
    """Return stable, value-free diagnostics for disallowed write sites."""
    offenders: list[str] = []
    for path in sorted(Path(item) for item in paths):
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        visitor = _SensitiveWriteVisitor(path)
        visitor.visit(tree)
        offenders.extend(visitor.offenders)
    return sorted(offenders)
