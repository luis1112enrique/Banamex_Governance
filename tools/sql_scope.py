"""Restrict generated SQL to business reads and enforce client row scope."""

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import Scope, traverse_scope

CLIENT_SCOPE_ENFORCED = True


class ClientScopeError(ValueError):
    pass


_RELATIONS = {
    ("dim", "dim_cliente"): True,
    ("cfg", "fact_sdos_captacoloca_m"): True,
    ("cfg", "fact_txn_captacoloca_d"): True,
    ("dim", "dim_producto"): False,
    ("dim", "dim_txn"): False,
}
_FUNCTIONS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "COALESCE", "NULLIF",
    "CAST", "TRY_CAST", "EXTRACT", "DATE_TRUNC", "TIMESTAMP_TRUNC", "TO_CHAR",
    "TIME_TO_STR", "ABS", "CEIL", "FLOOR", "LOWER", "UPPER", "TRIM",
    "SUBSTRING", "CONCAT", "LENGTH", "CURRENT_DATE", "CURRENT_TIMESTAMP",
    "ROW_NUMBER", "RANK", "DENSE_RANK", "LAG", "LEAD", "IF", "CASE", "AND", "OR",
    "TO_DATE", "STR_TO_DATE", "TO_TIMESTAMP", "STR_TO_TIME",
}
_TYPES = {
    "BIGINT", "INT", "SMALLINT", "DECIMAL", "FLOAT", "DOUBLE", "REAL",
    "TEXT", "VARCHAR", "CHAR", "BOOLEAN", "DATE", "TIMESTAMP", "TIMESTAMPTZ",
    "TIME", "TIMETZ", "INTERVAL",
}


def _business_query(sql: str, *, allow_cte: bool) -> tuple[exp.Query, list[exp.Table]]:
    try:
        statements = sqlglot.parse(sql, read="postgres")
    except sqlglot.errors.ParseError as exc:
        raise ClientScopeError("No se pudo verificar el alcance de la consulta.") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ClientScopeError("Solo se permiten consultas de lectura verificables.")
    tree = statements[0]
    forbidden = (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock, exp.Placeholder, exp.Parameter)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise ClientScopeError("La consulta contiene una operación fuera del alcance de lectura.")
    if any(with_clause.args.get("recursive") or not allow_cte for with_clause in tree.find_all(exp.With)):
        raise ClientScopeError("Esta consulta utiliza una construcción no habilitada para el perfil.")
    for function in tree.find_all(exp.Func):
        if isinstance(function.parent, exp.Dot):
            raise ClientScopeError("No se permiten funciones calificadas por un esquema externo.")
        name = function.name.upper() if isinstance(function, exp.Anonymous) else function.sql_name()
        if name not in _FUNCTIONS:
            raise ClientScopeError("La consulta contiene una función fuera del alcance autorizado.")
    for datatype in tree.find_all(exp.DataType):
        name = getattr(datatype.this, "value", str(datatype.this)).upper()
        if name not in _TYPES:
            raise ClientScopeError("La consulta utiliza un tipo de dato fuera del alcance autorizado.")
    # selected_sources resolves CTE references within each lexical scope. Merely
    # collecting every CTE alias globally would let an alias hide a real table
    # outside that scope, including schema-qualified system relations.
    tables = []
    try:
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table):
                    raise ClientScopeError("La consulta utiliza una fuente no verificable.")
                tables.append(source)
    except sqlglot.errors.OptimizeError as exc:
        raise ClientScopeError("No se pudo resolver el alcance de las fuentes de la consulta.") from exc
    if not tables:
        raise ClientScopeError("La consulta no utiliza una fuente autorizada.")
    for table in tables:
        key = (table.db.lower(), table.name.lower())
        if table.catalog or key not in _RELATIONS or not isinstance(table.this, exp.Identifier):
            raise ClientScopeError("La consulta intenta utilizar una fuente fuera del alcance autorizado.")
        for identifier in (table.this, table.args.get("db")):
            if not isinstance(identifier, exp.Identifier) or (identifier.quoted and identifier.name != identifier.name.lower()):
                raise ClientScopeError("La consulta utiliza un identificador de fuente no autorizado.")
    return tree, tables


def scope_business_sql(sql: str) -> str:
    """Allow business SELECTs and lexical, nonrecursive SELECT CTEs for stewards."""
    tree, _ = _business_query(sql, allow_cte=True)
    return tree.sql(dialect="postgres")


def scope_client_sql(sql: str, cliente_id: int) -> tuple[str, dict]:
    """Filter each physical client relation before joins and aggregation."""
    if type(cliente_id) is not int or cliente_id <= 0:
        raise ClientScopeError("La sesión no tiene un identificador de cliente válido.")
    tree, tables = _business_query(sql, allow_cte=False)
    for column in tree.find_all(exp.Column):
        if column.db:
            column.set("db", None)
    for table in tables:
        if _RELATIONS[(table.db.lower(), table.name.lower())]:
            source = table.copy()
            source.set("alias", None)
            scoped = exp.select("*").from_(source).where(
                exp.EQ(this=exp.column("num_cte"), expression=exp.Placeholder(this="scope_cliente_id"))
            ).subquery(alias=table.alias_or_name)
            table.replace(scoped)
    query = tree.sql(dialect="postgres").replace("%(scope_cliente_id)s", ":scope_cliente_id")
    return query, {"scope_cliente_id": cliente_id}
