#!/usr/bin/env python3
"""
init_db.py — Initialize the House KB database.
Run once to create the schema and set up the first admin user.

Usage:
    python3 init_db.py
    python3 init_db.py --db /path/to/house.db
"""

import sqlite3
import sys
import os
import argparse
import getpass
import hashlib
import secrets

DB_DEFAULT = os.path.join(os.path.dirname(__file__), "data", "house.db")
SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "schema.sql")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(32)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def init_db(db_path: str):
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    if os.path.exists(db_path):
        print(f"Database already exists at {db_path}")
        print("To re-initialize, delete the file first.")
        sys.exit(1)

    print(f"Creating database at {db_path}")
    with open(SCHEMA_FILE) as f:
        schema = f.read()

    conn = sqlite3.connect(db_path)
    conn.executescript(schema)

    # Seed root location
    conn.execute(
        "INSERT INTO locations (id, name, parent_id, notes) VALUES (1, 'Property', NULL, 'Root location')"
    )

    # Create first admin user
    print("\nCreate admin user:")
    username = input("  Username: ").strip()
    display_name = input("  Display name (e.g. Jane): ").strip()
    password = getpass.getpass("  Password: ")
    confirm = getpass.getpass("  Confirm password: ")

    if password != confirm:
        print("Passwords do not match.")
        conn.close()
        os.remove(db_path)
        sys.exit(1)

    pw_hash = hash_password(password)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?, ?, ?, 1)",
        (username, pw_hash, display_name),
    )
    conn.commit()
    conn.close()

    print(f"\nDatabase initialized.")
    print(f"  Path:       {db_path}")
    print(f"  Admin user: {username}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize House KB database")
    parser.add_argument("--db", default=DB_DEFAULT, help="Path to SQLite database file")
    args = parser.parse_args()
    init_db(args.db)
