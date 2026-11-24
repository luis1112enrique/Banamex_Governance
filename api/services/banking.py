"""Read-only presentation queries against the existing, authorized test model.

The facts contain account numbers and monthly registered balances. They do not
contain available balances, customer names, currency or a transaction direction.
Product/customer joins use observed keys; the facts declare no foreign keys.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import os
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor


class BankingUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503):
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _client_id(value: int) -> int:
    if type(value) is not int or value <= 0:
        raise BankingUnavailable("invalid_client", "La sesión no contiene un cliente autorizado.", 403)
    return value


def _currency() -> str | None:
    value = os.environ.get("BANAMEX_DISPLAY_CURRENCY", "").upper()
    return value if len(value) == 3 and value.isalpha() else None


def _cutoff(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return datetime.strptime(str(value), "%Y%m%d").date().isoformat()


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def _total(values: Iterator[Any]) -> float:
    return float(sum((Decimal(str(value or 0)) for value in values), Decimal("0")))


@contextmanager
def _cursor() -> Iterator[Any]:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    connection = None
    try:
        connection = psycopg2.connect(
            host=os.environ.get("PGHOST", "localhost"),
            port=os.environ.get("PGPORT", "5432"),
            dbname=os.environ.get("PGDATABASE", "reto_banamex"),
            user=os.environ.get("PGUSER", "postgres"),
            password=os.environ.get("PGPASSWORD", "banamex_database"),
            connect_timeout=5,
            options="-cdefault_transaction_read_only=on -cstatement_timeout=10000 -clock_timeout=3000",
        )
        connection.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                yield cursor
    except psycopg2.errors.QueryCanceled:
        raise BankingUnavailable("query_timeout", "PostgreSQL excedió el tiempo permitido para la consulta de lectura.") from None
    except psycopg2.errors.InsufficientPrivilege:
        raise BankingUnavailable("database_permissions", "La conexión de pruebas no tiene permiso de lectura para esta información.") from None
    except psycopg2.OperationalError:
        raise BankingUnavailable("database_connection", "No fue posible conectar con PostgreSQL del entorno de pruebas.") from None
    except psycopg2.Error:
        raise BankingUnavailable("database_query", "La consulta de lectura no pudo completarse en PostgreSQL.") from None
    finally:
        if connection is not None:
            connection.close()


def _ensure_client(cursor: Any, cliente_id: int) -> dict:
    cursor.execute(
        "SELECT num_cte, tipo_persona, segmento_cte FROM dim.dim_cliente WHERE num_cte = %s",
        (cliente_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise BankingUnavailable("client_not_found", "El cliente autorizado no existe en el entorno de pruebas.", 404)
    return {"segment": row["segmento_cte"], "person_type": row["tipo_persona"]}


def check_connection() -> bool:
    """Verify a real, read-only database round trip without returning financial data."""
    with _cursor() as cursor:
        cursor.execute("SELECT 1 AS available")
        return cursor.fetchone()["available"] == 1


def _accounts(cursor: Any, cliente_id: int) -> list[dict]:
    cursor.execute(
        """SELECT s.num_cta, s.cve_producto, s.saldo_mes, s.fec_alta,
                  s.fec_informacion, p.descripcion, p.cve_dominio, p.grupo_producto
           FROM cfg.fact_sdos_captacoloca_m AS s
           LEFT JOIN dim.dim_producto AS p ON p.cve_producto = s.cve_producto
           WHERE s.num_cte = %s
             AND s.fec_informacion = (
               SELECT MAX(fec_informacion) FROM cfg.fact_sdos_captacoloca_m WHERE num_cte = %s
             )
           ORDER BY s.cve_producto, s.num_cta""",
        (cliente_id, cliente_id),
    )
    result = []
    for row in cursor.fetchall():
        category = "other"
        if row["cve_producto"] == "AHORRO":
            category = "savings"
        elif row["grupo_producto"] == "CAPTACION":
            category = "debit"
        elif row["grupo_producto"] == "COLOCACION":
            category = "credit"
        result.append({
            "account_id": str(row["num_cta"]),
            "account_number": str(row["num_cta"]),
            "masked_number": "•••• " + str(row["num_cta"])[-4:],
            "product_code": row["cve_producto"],
            "product_name": row["descripcion"] or row["cve_producto"],
            "domain": row["cve_dominio"], "group": row["grupo_producto"],
            "category": category,
            "registered_balance": _number(row["saldo_mes"]),
            "available_balance": None,
            "opened_on": _cutoff(row["fec_alta"]),
            "cutoff_date": _cutoff(row["fec_informacion"]),
        })
    return result


def _monthly_balances(cursor: Any, cliente_id: int | None = None) -> list[dict]:
    scope = "WHERE s.num_cte = %s" if cliente_id is not None else ""
    cursor.execute(
        """SELECT s.fec_informacion,
                  SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'CAPTACION') AS deposit_balance,
                  SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'COLOCACION') AS credit_balance
           FROM cfg.fact_sdos_captacoloca_m AS s
           JOIN dim.dim_producto AS p ON p.cve_producto = s.cve_producto
           """ + scope + " GROUP BY s.fec_informacion ORDER BY s.fec_informacion",
        (cliente_id,) if cliente_id is not None else (),
    )
    return [{
        "cutoff_date": _cutoff(row["fec_informacion"]),
        "deposit_balance": _number(row["deposit_balance"]),
        "credit_balance": _number(row["credit_balance"]),
    } for row in cursor.fetchall()]


def _limitations() -> list[str]:
    items = [
        "Los importes son saldos registrados al cierre; el modelo no incluye saldo disponible.",
        "El modelo no contiene productos de inversión ni nombre personal del cliente.",
        "Los movimientos no indican sentido de cargo o abono; no se infiere un signo.",
    ]
    if _currency() is None:
        items.append("La moneda no está especificada en el modelo de datos.")
    return items


def get_accounts(cliente_id: int) -> dict:
    cliente_id = _client_id(cliente_id)
    with _cursor() as cursor:
        _ensure_client(cursor, cliente_id)
        accounts = _accounts(cursor, cliente_id)
    return {
        "cutoff_date": accounts[0]["cutoff_date"] if accounts else None,
        "currency": _currency(), "accounts": accounts, "limitations": _limitations(),
    }


def get_products(cliente_id: int) -> dict:
    account_data = get_accounts(cliente_id)
    products: dict[str, dict] = {}
    for account in account_data["accounts"]:
        code = account["product_code"]
        if code not in products:
            products[code] = {key: account[key] for key in (
                "product_code", "product_name", "domain", "group", "category",
            )}
            products[code].update(account_count=0, registered_balance=0.0, available_balance=None)
        products[code]["account_count"] += 1
        products[code]["registered_balance"] = _total(iter((
            products[code]["registered_balance"], account["registered_balance"],
        )))
    return {
        "cutoff_date": account_data["cutoff_date"], "currency": _currency(),
        "products": list(products.values()), "unavailable_categories": ["investments"],
        "limitations": _limitations(),
    }


def get_summary(cliente_id: int) -> dict:
    cliente_id = _client_id(cliente_id)
    with _cursor() as cursor:
        client = _ensure_client(cursor, cliente_id)
        accounts = _accounts(cursor, cliente_id)
        monthly = _monthly_balances(cursor, cliente_id)
        cursor.execute(
            """SELECT desc_txn, SUM(saldo_txn) AS amount, COUNT(*) AS count
               FROM cfg.fact_txn_captacoloca_d WHERE num_cte = %s
               GROUP BY desc_txn ORDER BY SUM(saldo_txn) DESC, desc_txn""",
            (cliente_id,),
        )
        concepts = [{"description": row["desc_txn"], "amount": _number(row["amount"]),
                     "count": row["count"]} for row in cursor.fetchall()]
    deposits = [item for item in accounts if item["group"] == "CAPTACION"]
    credits = [item for item in accounts if item["group"] == "COLOCACION"]
    return {
        "cliente_id": cliente_id, "client": client,
        "cutoff_date": accounts[0]["cutoff_date"] if accounts else None,
        "currency": _currency(),
        "registered_balance": _total(item["registered_balance"] for item in deposits) if deposits else None,
        "credit_balance": _total(item["registered_balance"] for item in credits) if credits else None,
        "available_balance": None, "account_count": len(accounts),
        "product_count": len({item["product_code"] for item in accounts}),
        "monthly_balances": monthly, "transaction_concepts": concepts,
        "limitations": _limitations(),
    }


def get_transactions(cliente_id: int, limit: int = 20, offset: int = 0) -> dict:
    cliente_id = _client_id(cliente_id)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise BankingUnavailable("invalid_pagination", "La paginación solicitada no es válida.", 400)
    with _cursor() as cursor:
        _ensure_client(cursor, cliente_id)
        cursor.execute(
            """SELECT COUNT(*) AS count, MIN(fec_txn) AS date_from, MAX(fec_txn) AS date_to,
                      MAX(fec_informacion) AS cutoff
               FROM cfg.fact_txn_captacoloca_d WHERE num_cte = %s""",
            (cliente_id,),
        )
        coverage = cursor.fetchone()
        cursor.execute(
            """SELECT t.num_cta, t.cve_producto, p.descripcion, t.fec_txn,
                      t.saldo_txn, t.cve_txn, t.desc_txn, t.fec_informacion
               FROM cfg.fact_txn_captacoloca_d AS t
               LEFT JOIN dim.dim_producto AS p ON p.cve_producto = t.cve_producto
               WHERE t.num_cte = %s
               ORDER BY t.fec_txn DESC, t.num_cta, t.cve_txn, t.saldo_txn, t.desc_txn
               LIMIT %s OFFSET %s""",
            (cliente_id, limit, offset),
        )
        transactions = [{
            "id": f"{row['num_cta']}:{row['fec_txn']}:{row['cve_txn']}:{offset + index}",
            "account_id": str(row["num_cta"]), "product_code": row["cve_producto"],
            "product_name": row["descripcion"] or row["cve_producto"],
            "date": _cutoff(row["fec_txn"]), "description": row["desc_txn"],
            "amount": _number(row["saldo_txn"]), "direction": None,
            "cutoff_date": _cutoff(row["fec_informacion"]),
        } for index, row in enumerate(cursor.fetchall())]
    return {
        "cutoff_date": _cutoff(coverage["cutoff"]), "currency": _currency(),
        "transaction_date_from": _cutoff(coverage["date_from"]),
        "transaction_date_to": _cutoff(coverage["date_to"]),
        "transactions": transactions, "total": coverage["count"], "limit": limit, "offset": offset,
        "limitations": ["Importes registrados; el modelo no indica cargo o abono."],
    }


def get_institution_summary() -> dict:
    with _cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS count FROM dim.dim_cliente")
        registered_clients = cursor.fetchone()["count"]
        cursor.execute("SELECT MAX(fec_informacion) AS cutoff FROM cfg.fact_sdos_captacoloca_m")
        cutoff = cursor.fetchone()["cutoff"]
        cursor.execute(
            """SELECT COUNT(DISTINCT s.num_cte) AS client_count,
                      COUNT(DISTINCT s.num_cta) AS account_count,
                      SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'CAPTACION') AS deposit_balance,
                      SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'COLOCACION') AS credit_balance
               FROM cfg.fact_sdos_captacoloca_m AS s
               JOIN dim.dim_producto AS p ON p.cve_producto = s.cve_producto
               WHERE s.fec_informacion = %s""", (cutoff,),
        )
        totals = cursor.fetchone()
        cursor.execute(
            """SELECT s.cve_producto, p.descripcion, p.grupo_producto,
                      SUM(s.saldo_mes) AS balance, COUNT(DISTINCT s.num_cta) AS account_count
               FROM cfg.fact_sdos_captacoloca_m AS s
               JOIN dim.dim_producto AS p ON p.cve_producto = s.cve_producto
               WHERE s.fec_informacion = %s
               GROUP BY s.cve_producto, p.descripcion, p.grupo_producto ORDER BY s.cve_producto""", (cutoff,),
        )
        products = [{"product_code": row["cve_producto"], "product_name": row["descripcion"],
                     "group": row["grupo_producto"], "registered_balance": _number(row["balance"]),
                     "account_count": row["account_count"]} for row in cursor.fetchall()]
        cursor.execute(
            """SELECT c.segmento_cte, COUNT(DISTINCT s.num_cte) AS client_count,
                      SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'CAPTACION') AS deposit_balance,
                      SUM(s.saldo_mes) FILTER (WHERE p.grupo_producto = 'COLOCACION') AS credit_balance
               FROM cfg.fact_sdos_captacoloca_m AS s
               JOIN dim.dim_producto AS p ON p.cve_producto = s.cve_producto
               JOIN dim.dim_cliente AS c ON c.num_cte = s.num_cte
               WHERE s.fec_informacion = %s GROUP BY c.segmento_cte ORDER BY c.segmento_cte""", (cutoff,),
        )
        segments = [{"segment": row["segmento_cte"], "client_count": row["client_count"],
                     "deposit_balance": _number(row["deposit_balance"]),
                     "credit_balance": _number(row["credit_balance"])} for row in cursor.fetchall()]
        monthly = _monthly_balances(cursor)
    return {
        "cutoff_date": _cutoff(cutoff), "currency": _currency(),
        "registered_clients": registered_clients, "active_clients": totals["client_count"],
        "account_count": totals["account_count"],
        "deposit_balance": _number(totals["deposit_balance"]),
        "credit_balance": _number(totals["credit_balance"]),
        "product_breakdown": products, "segment_breakdown": segments,
        "monthly_balances": monthly, "limitations": _limitations(),
    }
