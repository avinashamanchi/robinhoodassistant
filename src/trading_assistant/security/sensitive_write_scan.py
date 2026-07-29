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

def _default_registry() -> tuple[
    dict[str, tuple[str, frozenset[str]]],
    dict[str, frozenset[str]],
]:
    from ..db.models import Base
    from .sensitive_fields import SENSITIVE_FIELDS

    registered: dict[str, tuple[str, frozenset[str]]] = {}
    for mapper in Base.registry.mappers:
        table = mapper.local_table.name
        if table in SENSITIVE_FIELDS:
            registered[mapper.class_.__name__] = (
                table,
                frozenset(SENSITIVE_FIELDS[table]),
            )
    return (
        registered,
        {
            table: frozenset(fields)
            for table, fields in SENSITIVE_FIELDS.items()
        },
    )


_DML = re.compile(r"\b(?:delete|insert|update|replace)\b", re.I)


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
    def __init__(
        self,
        path: Path,
        *,
        model_fields: dict[str, tuple[str, frozenset[str]]],
        table_fields: dict[str, frozenset[str]],
    ) -> None:
        self.path = path
        self.model_fields = model_fields
        self.table_fields = table_fields
        self.unique_fields = (
            set().union(
                *(
                    fields
                    for _, fields in self.model_fields.values()
                )
            )
            - {"reason"}
            if self.model_fields
            else set()
        )
        self.offenders: list[str] = []
        self.class_aliases = {name: name for name in self.model_fields}
        self.sql_builders = {
            "select": "select",
            "insert": "insert",
            "update": "update",
            "delete": "delete",
        }
        self.object_models: dict[str, str] = {}
        self.unknown_object_aliases: set[str] = set()
        self.query_models: dict[str, str] = {}
        self.statement_models: dict[str, str] = {}
        self.mutation_call_models: dict[str, str] = {}
        self.execute_aliases: set[str] = set()
        self.structured_statements: set[str] = set()
        self.string_constants: dict[str, str] = {}
        self.local_functions: dict[
            str,
            ast.FunctionDef | ast.AsyncFunctionDef,
        ] = {}

    def visit_Module(self, node: ast.Module) -> None:
        self.local_functions = {
            statement.name: statement
            for statement in node.body
            if isinstance(
                statement,
                (ast.FunctionDef, ast.AsyncFunctionDef),
            )
        }
        self.generic_visit(node)

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
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"scalar", "scalars", "execute"}
            and node.args
        ):
            return self._selection_model(node.args[0])
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr
            in {"one", "one_or_none", "first", "scalar_one", "scalar_one_or_none"}
        ):
            return self._call_model(node.func.value)
        inferred = {
            self._model(argument)
            for argument in node.args
            if self._model(argument) is not None
        }
        if len(inferred) == 1:
            return next(iter(inferred))
        return None

    def _query_model(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return self.query_models.get(node.id)
        if not isinstance(node, ast.Call):
            return None
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "query"
            and node.args
        ):
            return self._model(node.args[0])
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "filter",
            "filter_by",
            "join",
            "limit",
            "offset",
            "order_by",
            "where",
        }:
            return self._query_model(node.func.value)
        return None

    def _table_model(self, node: ast.AST | None) -> str | None:
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__table__"
        ):
            return self._model(node.value)
        return None

    def _selection_model(self, node: ast.AST | None) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            query_model = self._query_model(node.func.value)
            if query_model is not None:
                return query_model
            table_model = self._table_model(node.func.value)
            if table_model is not None:
                return table_model
        if (
            self.sql_builders.get(_name(node.func) or "") == "select"
            and node.args
        ):
            return self._model(node.args[0])
        if isinstance(node.func, ast.Attribute):
            return self._selection_model(node.func.value)
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
        if (
            self.sql_builders.get(_name(node.func) or "")
            in {"delete", "insert", "update"}
            and node.args
        ):
            return self._model(node.args[0])
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            return self._table_model(node.func.value)
        return None

    def _mutation_kind(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            model = self.statement_models.get(node.id)
            if node.id in self.mutation_call_models:
                return "update"
            return "unknown" if model is not None else None
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
            return self._mutation_kind(node.func.value)
        kind = self.sql_builders.get(_name(node.func) or "")
        return kind if kind in {"delete", "insert", "update"} else None

    def _structured_sql(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return (
                node.id in self.structured_statements
                or node.id in self.statement_models
            )
        if not isinstance(node, ast.Call):
            return False
        if self.sql_builders.get(_name(node.func) or "") in {
            "select",
            "insert",
            "update",
            "delete",
        }:
            return True
        if isinstance(node.func, ast.Attribute):
            return self._structured_sql(node.func.value)
        return False

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
        sensitive = self.model_fields[model][1]
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

    def _check_raw_sql(
        self,
        node: ast.AST,
        expression: ast.AST,
    ) -> None:
        sql = self._constant_string(expression)
        if sql is None:
            if self._structured_sql(expression):
                return
            self._report(node, "dynamic_sql", "**expression")
            return
        if _DML.search(sql) is None:
            return
        lowered = sql.lower()
        for table, fields in self.table_fields.items():
            if re.search(rf"\b{re.escape(table.lower())}\b", lowered) is None:
                continue
            if re.search(r"\bdelete\b", lowered):
                self._report(node, table, "**row")
            for field in sorted(fields):
                if re.search(
                    rf"\b{re.escape(field.lower())}\b",
                    lowered,
                ):
                    self._report(node, table, field)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            if imported.name in self.model_fields:
                self.class_aliases[
                    imported.asname or imported.name
                ] = imported.name
            if imported.name in self.sql_builders:
                self.sql_builders[
                    imported.asname or imported.name
                ] = self.sql_builders[imported.name]
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
        object_model = self._object_model(value) or self._call_model(value)
        annotated = self._model_name_from_annotation(annotation)
        if object_model is not None or annotated is not None:
            self.object_models[target.id] = object_model or annotated  # type: ignore[assignment]
            self.unknown_object_aliases.discard(target.id)
        elif (
            isinstance(value, ast.Name)
            and value.id in self.unknown_object_aliases
        ):
            self.unknown_object_aliases.add(target.id)
        elif isinstance(value, ast.Call):
            self.unknown_object_aliases.add(target.id)
        query_model = self._query_model(value)
        if query_model is not None:
            self.query_models[target.id] = query_model
        statement_model = self._mutation_model(value)
        if statement_model is not None:
            self.statement_models[target.id] = statement_model
        if self._structured_sql(value):
            self.structured_statements.add(target.id)
        if isinstance(value, ast.Attribute):
            if value.attr in {"execute", "exec_driver_sql"}:
                self.execute_aliases.add(target.id)
            if value.attr == "update":
                mutation_model = (
                    self._query_model(value.value)
                    or self._table_model(value.value)
                )
                if mutation_model is not None:
                    self.mutation_call_models[target.id] = mutation_model
        elif isinstance(value, ast.Name):
            if value.id in self.execute_aliases:
                self.execute_aliases.add(target.id)
            if value.id in self.mutation_call_models:
                self.mutation_call_models[target.id] = (
                    self.mutation_call_models[value.id]
                )
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

    @staticmethod
    def _keyword(
        node: ast.Call,
        name: str,
    ) -> ast.AST | None:
        return next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg == name
            ),
            None,
        )

    def _inspect_local_helper(
        self,
        helper: ast.FunctionDef | ast.AsyncFunctionDef,
        bindings: dict[str, str],
        *,
        active: set[str],
    ) -> None:
        if helper.name in active:
            self._report(helper, "unknown_model", "**helper_flow")
            return
        active.add(helper.name)
        local_bindings = dict(bindings)
        changed = True
        while changed:
            changed = False
            for statement in ast.walk(helper):
                if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    continue
                value = statement.value
                source_model = (
                    local_bindings.get(value.id)
                    if isinstance(value, ast.Name)
                    else self._call_model(value)
                )
                if source_model is None:
                    continue
                for target in (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                ):
                    if (
                        isinstance(target, ast.Name)
                        and local_bindings.get(target.id) != source_model
                    ):
                        local_bindings[target.id] = source_model
                        changed = True
        for statement in ast.walk(helper):
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
                for target in targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                    ):
                        continue
                    model = local_bindings.get(target.value.id)
                    if (
                        model is not None
                        and target.attr in self.model_fields[model][1]
                    ):
                        self._report(target, model, target.attr)
            if not isinstance(statement, ast.Call):
                continue
            if _name(statement.func) == "setattr" and len(statement.args) >= 2:
                receiver = statement.args[0]
                model = (
                    local_bindings.get(receiver.id)
                    if isinstance(receiver, ast.Name)
                    else None
                )
                field = (
                    statement.args[1].value
                    if isinstance(statement.args[1], ast.Constant)
                    and isinstance(statement.args[1].value, str)
                    else None
                )
                if model is not None and field in self.model_fields[model][1]:
                    self._report(statement, model, field)
                elif model is not None and field is None:
                    self._report(statement, model, "**field")
            nested = (
                self.local_functions.get(statement.func.id)
                if isinstance(statement.func, ast.Name)
                else None
            )
            if nested is None:
                continue
            nested_arguments = (
                list(nested.args.posonlyargs)
                + list(nested.args.args)
                + list(nested.args.kwonlyargs)
            )
            nested_by_name = {
                parameter.arg: parameter
                for parameter in nested_arguments
            }
            nested_bindings: dict[str, str] = {}
            for parameter, argument in zip(
                (
                    list(nested.args.posonlyargs)
                    + list(nested.args.args)
                ),
                statement.args,
            ):
                model = (
                    local_bindings.get(argument.id)
                    if isinstance(argument, ast.Name)
                    else self._object_model(argument)
                    or self._call_model(argument)
                )
                if model is not None:
                    nested_bindings[parameter.arg] = model
            for keyword in statement.keywords:
                if (
                    keyword.arg is None
                    or keyword.arg not in nested_by_name
                ):
                    if keyword.arg is None:
                        self._report(
                            statement,
                            "unknown_model",
                            "**helper_flow",
                        )
                    continue
                model = (
                    local_bindings.get(keyword.value.id)
                    if isinstance(keyword.value, ast.Name)
                    else self._object_model(keyword.value)
                    or self._call_model(keyword.value)
                )
                if model is not None:
                    nested_bindings[keyword.arg] = model
            if nested_bindings:
                self._inspect_local_helper(
                    nested,
                    nested_bindings,
                    active=active,
                )
        active.remove(helper.name)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Attribute):
                model = self._object_model(target.value)
                if (
                    model is not None
                    and target.attr in self.model_fields[model][1]
                ):
                    self._report(target, model, target.attr)
                elif target.attr in self.unique_fields:
                    self._report(target, "*", target.attr)
                elif (
                    isinstance(target.value, ast.Name)
                    and target.value.id in self.unknown_object_aliases
                    and target.attr
                    in set().union(
                        *(
                            fields
                            for _, fields in self.model_fields.values()
                        )
                    )
                ):
                    self._report(target, "unknown_model", target.attr)
            self._record_target(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Attribute):
            model = self._object_model(node.target.value)
            if (
                model is not None
                and node.target.attr in self.model_fields[model][1]
            ):
                self._report(node.target, model, node.target.attr)
            elif node.target.attr in self.unique_fields:
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
                and node.target.attr in self.model_fields[model][1]
            ):
                self._report(node.target, model, node.target.attr)
            elif node.target.attr in self.unique_fields:
                self._report(node.target, "*", node.target.attr)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        prior_objects = dict(self.object_models)
        prior_unknown_objects = set(self.unknown_object_aliases)
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
        self.unknown_object_aliases = prior_unknown_objects

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
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
        ):
            query_model = self._query_model(node.func.value)
            if query_model is not None:
                values_mapping = self._keyword(node, "values")
                self._check_mapping(
                    node,
                    query_model,
                    args=(
                        (*node.args, values_mapping)
                        if values_mapping is not None
                        else node.args
                    ),
                    keywords=(
                        keyword
                        for keyword in node.keywords
                        if keyword.arg != "values"
                    ),
                )
        if (
            isinstance(node.func, ast.Name)
            and node.func.id in self.mutation_call_models
        ):
            values_mapping = self._keyword(node, "values")
            self._check_mapping(
                node,
                self.mutation_call_models[node.func.id],
                args=(
                    (*node.args, values_mapping)
                    if values_mapping is not None
                    else node.args
                ),
                keywords=(
                    keyword
                    for keyword in node.keywords
                    if keyword.arg != "values"
                ),
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

        approved_helpers = {
            "select",
            "insert",
            "update",
            "delete",
            "get",
            "merge",
            "bulk_insert_mappings",
            "bulk_update_mappings",
            "join",
            "outerjoin",
            "select_from",
            "where",
            "order_by",
            "group_by",
            "having",
            "limit",
            "offset",
            "options",
            "filter",
            "filter_by",
            "distinct",
            "returning",
            "execution_options",
            "on_conflict_do_update",
            "values",
        }
        if call_name not in approved_helpers:
            for index, argument in enumerate(node.args):
                helper_model = self._model(argument)
                if helper_model is None:
                    continue
                trailing = node.args[index + 1 :]
                if trailing or node.keywords:
                    self._check_mapping(
                        node,
                        helper_model,
                        args=trailing,
                        keywords=node.keywords,
                    )
                break

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
                and field in self.model_fields[object_model][1]
            ):
                self._report(node, object_model, field)
            elif object_model is not None and field is None:
                self._report(node, object_model, "**field")
            elif field in self.unique_fields:
                self._report(node, "*", field)

        execution_call = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "exec_driver_sql"}
        ) or (
            isinstance(node.func, ast.Name)
            and node.func.id in self.execute_aliases
        )
        statement = (
            node.args[0]
            if node.args
            else self._keyword(node, "statement")
            if execution_call
            else None
        )
        if execution_call and statement is not None:
            execute_model = self._mutation_model(statement)
            if (
                execute_model is not None
                and self._mutation_kind(statement) == "delete"
            ):
                self._report(node, execute_model, "**row")
            parameter_mappings = list(node.args[1:])
            for parameter_name in ("params", "parameters"):
                parameter = self._keyword(node, parameter_name)
                if parameter is not None:
                    parameter_mappings.append(parameter)
            if execute_model is not None and parameter_mappings:
                self._check_mapping(
                    node,
                    execute_model,
                    args=parameter_mappings,
                    keywords=(
                        keyword
                        for keyword in node.keywords
                        if keyword.arg
                        not in {"statement", "params", "parameters"}
                    ),
                )
            self._check_raw_sql(node, statement)

        local_helper = (
            self.local_functions.get(node.func.id)
            if isinstance(node.func, ast.Name)
            else None
        )
        if local_helper is not None:
            parameters = (
                list(local_helper.args.posonlyargs)
                + list(local_helper.args.args)
                + list(local_helper.args.kwonlyargs)
            )
            bindings: dict[str, str] = {}
            for parameter, argument in zip(parameters, node.args):
                model = self._object_model(argument) or self._call_model(
                    argument
                )
                if model is not None:
                    bindings[parameter.arg] = model
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue
                model = self._object_model(keyword.value) or self._call_model(
                    keyword.value
                )
                if model is not None:
                    bindings[keyword.arg] = model
            if bindings:
                self._inspect_local_helper(
                    local_helper,
                    bindings,
                    active=set(),
                )

        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "delete"
            and node.args
        ):
            object_model = self._object_model(node.args[0])
            canonical_store = (
                isinstance(node.func.value, ast.Call)
                and _name(node.func.value.func) == "sensitive_store"
            )
            if object_model is not None and not canonical_store:
                self._report(node, object_model, "**row")

        self.generic_visit(node)


def scan_sensitive_writes(
    paths: Iterable[Path],
    *,
    model_fields: dict[str, tuple[str, frozenset[str]]] | None = None,
    table_fields: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """Return stable, value-free diagnostics for disallowed write sites."""
    if model_fields is None or table_fields is None:
        default_models, default_tables = _default_registry()
        model_fields = (
            default_models if model_fields is None else model_fields
        )
        table_fields = (
            default_tables if table_fields is None else table_fields
        )
    offenders: list[str] = []
    for path in sorted(Path(item) for item in paths):
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )
        visitor = _SensitiveWriteVisitor(
            path,
            model_fields=model_fields,
            table_fields=table_fields,
        )
        visitor.visit(tree)
        offenders.extend(visitor.offenders)
    return sorted(offenders)
