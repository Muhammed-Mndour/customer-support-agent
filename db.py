"""Create the fake store database. Running this file resets all the data."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "store.db"

SCHEMA = """
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE
);

CREATE TABLE orders (
    id                INTEGER PRIMARY KEY,
    customer_id       INTEGER NOT NULL REFERENCES customers(id),
    item              TEXT NOT NULL,
    amount            INTEGER NOT NULL,  -- in Egyptian pounds
    status            TEXT NOT NULL
                      CHECK (status IN ('delivered', 'shipped', 'delayed', 'cancelled')),
    order_date        TEXT NOT NULL,
    expected_delivery TEXT,              -- empty for cancelled orders
    refunded          INTEGER NOT NULL DEFAULT 0  -- 0 = no, 1 = yes
);

CREATE TABLE tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER REFERENCES customers(id),  -- empty for an unknown customer
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""

CUSTOMERS = [
    (1, "Mohamed Ali", "01012345678"),
    (2, "Mona Ibrahim", "01098765432"),
    (3, "Mahmoud Saeed", "01123456789"),
    (4, "Yasmin Khaled", "01234567890"),
    (5, "Karim Mostafa", "01555123456"),
]

# (id, customer_id, item, amount, status, days_ago_ordered, days_until_delivery)
# Dates are relative to today, so a delayed order always looks current.
ORDERS = [
    (1001, 1, "Wireless Earbuds", 850, "delivered", 30, -24),
    (1002, 2, "Bed Sheets", 450, "delivered", 25, -19),
    (1003, 4, "Coffee Maker", 1800, "delivered", 20, -14),
    (1004, 1, "Desk Lamp", 650, "delayed", 12, 3),
    (1005, 2, "Air Fryer", 2400, "delayed", 10, 4),
    (1006, 4, "Laptop Stand", 400, "delayed", 9, 5),
    (1007, 3, "Running Shoes", 1200, "shipped", 5, 2),
    (1008, 5, "Smart Watch", 3200, "shipped", 4, 3),
    (1009, 3, "Backpack", 350, "cancelled", 15, None),
    (1010, 5, "USB Cable", 120, "cancelled", 14, None),
]


def get_connection():
    """Open the database. Rows can be read by column name, like row["phone"]."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_database():
    """Build empty tables, then fill them with the fake data."""
    today = date.today()
    with get_connection() as connection:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO customers VALUES (?, ?, ?)", CUSTOMERS)
        for order_id, customer_id, item, amount, status, days_ago, days_until in ORDERS:
            order_date = today - timedelta(days=days_ago)
            expected = None if days_until is None else today + timedelta(days=days_until)
            connection.execute(
                "INSERT INTO orders (id, customer_id, item, amount, status,"
                " order_date, expected_delivery) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (order_id, customer_id, item, amount, status,
                 order_date.isoformat(), expected.isoformat() if expected else None),
            )
    connection.close()


def print_table(title, query):
    """Print the result of a query as a simple aligned table."""
    connection = get_connection()
    rows = connection.execute(query).fetchall()
    connection.close()
    print(f"\n=== {title} ({len(rows)} rows) ===")
    if not rows:
        print("(empty)")
        return
    headers = rows[0].keys()
    cells = [[str(row[h]) if row[h] is not None else "-" for h in headers] for row in rows]
    widths = [max(len(h), *(len(line[i]) for line in cells)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for line in cells:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths)))


if __name__ == "__main__":
    create_database()
    print(f"Database created: {DB_PATH.name}")
    print_table("customers", "SELECT * FROM customers")
    print_table(
        "orders",
        "SELECT o.id, c.name AS customer, o.item, o.amount, o.status,"
        " o.order_date, o.expected_delivery"
        " FROM orders o JOIN customers c ON c.id = o.customer_id ORDER BY o.id",
    )
    print_table("tickets", "SELECT * FROM tickets")
