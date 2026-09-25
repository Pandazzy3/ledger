"""One-time migration: add accounts table and account_id columns."""
from app import app, db
from sqlalchemy import text, inspect

with app.app_context():
    db.create_all()  # creates the accounts table if missing
    inspector = inspect(db.engine)

    exp_cols = {c["name"] for c in inspector.get_columns("expenses")}
    rec_cols = {c["name"] for c in inspector.get_columns("recurring_rules")}

    with db.engine.connect() as conn:
        if "account_id" not in exp_cols:
            conn.execute(text("ALTER TABLE expenses ADD COLUMN account_id INTEGER REFERENCES accounts(id)"))
            conn.commit()
            print("+ expenses.account_id")
        else:
            print("  ok expenses.account_id")

        if "account_id" not in rec_cols:
            conn.execute(text("ALTER TABLE recurring_rules ADD COLUMN account_id INTEGER REFERENCES accounts(id)"))
            conn.commit()
            print("+ recurring_rules.account_id")
        else:
            print("  ok recurring_rules.account_id")

    print("Done.")