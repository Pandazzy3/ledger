"""Safe migration: add missing columns to users table."""
from app import app, db
from sqlalchemy import text, inspect

with app.app_context():
    inspector = inspect(db.engine)
    cols = {c["name"] for c in inspector.get_columns("users")}
    print("Current columns:", sorted(cols))

    migrations = [
        ("language",      "VARCHAR(10) DEFAULT 'en'"),
        ("currency",      "VARCHAR(3)  DEFAULT 'USD'"),
        ("base_currency", "VARCHAR(3)  DEFAULT 'USD'"),
    ]

    with db.engine.connect() as conn:
        for col, definition in migrations:
            if col in cols:
                print(f"✓ {col} already exists")
                continue
            try:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))
                conn.commit()
                print(f"+ added {col}")
            except Exception as e:
                print(f"✗ {col}: {e}")

    # Verify
    inspector = inspect(db.engine)
    new_cols = {c["name"] for c in inspector.get_columns("users")}
    print("After migration:", sorted(new_cols))