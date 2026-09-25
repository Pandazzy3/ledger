"""AI budget assistant with multi-provider fallback and function calling."""
import os
import time
import json
from datetime import date, datetime, timedelta
from calendar import monthrange

from sqlalchemy import func

from models import (
    db, Account, Expense, Category, SavingsGoal, AiChat,
)
from translations import SUPPORTED_CURRENCIES
from exchange_rates import fetch_rates, convert as convert_currency, build_rate_board


# ---------------- FINANCIAL SNAPSHOT ----------------

def build_financial_snapshot(user_id):
    from models import User
    user = db.session.get(User, user_id)

    today = date.today()
    month_start = today.replace(day=1)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)

    def totals(start, end):
        inc = (db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
               .filter(Expense.user_id == user_id, Expense.kind == "income",
                       Expense.date >= start, Expense.date <= end).scalar() or 0.0)
        exp = (db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
               .filter(Expense.user_id == user_id, Expense.kind == "expense",
                       Expense.date >= start, Expense.date <= end).scalar() or 0.0)
        return round(float(inc), 2), round(float(exp), 2)

    this_inc, this_exp = totals(month_start, month_end)
    prev_inc, prev_exp = totals(prev_start, prev_end)

    by_cat_rows = (db.session.query(Expense.category, func.sum(Expense.amount))
                   .filter(Expense.user_id == user_id, Expense.kind == "expense",
                           Expense.date >= month_start, Expense.date <= month_end)
                   .group_by(Expense.category)
                   .order_by(func.sum(Expense.amount).desc()).all())
    by_cat = [{"category": r[0], "amount": round(float(r[1]), 2)} for r in by_cat_rows]

    # Accounts
    accounts = Account.query.filter_by(user_id=user_id, archived=False).order_by(Account.name).all()
    accounts_list = [{
        "id": a.id, "name": a.name, "kind": a.kind,
        "balance": round(a.balance, 2), "currency": a.currency,
    } for a in accounts]

    cats = Category.query.filter_by(user_id=user_id).all()
    categories_list = [{"id": c.id, "name": c.name, "kind": c.kind,
                        "monthly_budget": c.monthly_budget} for c in cats]

    goals = [{"id": g.id, "name": g.name, "target": g.target_amount,
              "current": g.current_amount,
              "deadline": g.deadline.isoformat() if g.deadline else None,
              "days_left": g.days_left}
             for g in SavingsGoal.query.filter_by(user_id=user_id).all()]

    recent = (Expense.query.filter_by(user_id=user_id)
              .order_by(Expense.date.desc(), Expense.id.desc()).limit(15).all())
    recent_list = [{"id": e.id, "date": e.date.isoformat(), "kind": e.kind,
                    "amount": e.amount, "category": e.category,
                    "description": e.description or "",
                    "account_id": e.account_id,
                    "account_name": e.account.name if e.account else None}
                   for e in recent]

    return {
        "today": today.isoformat(),
        "base_currency": user.base_currency if user else "USD",
        "display_currency": user.currency if user else "USD",
        "language": user.language if user else "en",
        "accounts": accounts_list,
        "this_month": {"label": month_start.strftime("%B %Y"),
                       "income": this_inc, "expenses": this_exp,
                       "net": round(this_inc - this_exp, 2),
                       "by_category": by_cat},
        "last_month": {"label": prev_start.strftime("%B %Y"),
                       "income": prev_inc, "expenses": prev_exp,
                       "net": round(prev_inc - prev_exp, 2)},
        "categories": categories_list,
        "savings_goals": goals,
        "recent_expenses": recent_list,
    }


def format_snapshot_for_prompt(s):
    lines = [f"Today: {s['today']}"]
    lines.append(f"User's stored currency: {s['base_currency']}")
    lines.append(f"User's display currency: {s['display_currency']}")
    lines.append(f"User's UI language: {s['language']}")
    lines.append("")
    if s['accounts']:
        lines.append("=== ACCOUNTS (with IDs) ===")
        for a in s['accounts']:
            lines.append(f"  [{a['id']}] {a['name']} ({a['kind']}) — {a['balance']:.2f} {a['currency']}")
        lines.append("")
    lines.append(f"=== THIS MONTH ({s['this_month']['label']}) ===")
    lines.append(f"Income: {s['this_month']['income']:.2f} {s['base_currency']}, "
                 f"Expenses: {s['this_month']['expenses']:.2f} {s['base_currency']}, "
                 f"Net: {s['this_month']['net']:+.2f} {s['base_currency']}")
    if s['this_month']['by_category']:
        lines.append("Spending by category:")
        for c in s['this_month']['by_category']:
            lines.append(f"  - {c['category']}: {c['amount']:.2f}")
    lines.append("")
    lines.append(f"=== LAST MONTH ({s['last_month']['label']}) ===")
    lines.append(f"Income: {s['last_month']['income']:.2f}, "
                 f"Expenses: {s['last_month']['expenses']:.2f}, "
                 f"Net: {s['last_month']['net']:+.2f}")
    lines.append("")
    if s['categories']:
        lines.append("=== CATEGORIES (with IDs) ===")
        for c in s['categories']:
            b = f", budget {c['monthly_budget']:.2f}" if c['monthly_budget'] else ""
            lines.append(f"  [{c['id']}] {c['name']} ({c['kind']}{b})")
        lines.append("")
    if s['savings_goals']:
        lines.append("=== SAVINGS GOALS (with IDs) ===")
        for g in s['savings_goals']:
            dl = f", deadline {g['deadline']} ({g['days_left']}d left)" if g['deadline'] else ""
            lines.append(f"  [{g['id']}] {g['name']}: {g['current']:.2f} / {g['target']:.2f}{dl}")
        lines.append("")
    if s['recent_expenses']:
        lines.append("=== RECENT ENTRIES (with IDs, newest first) ===")
        for e in s['recent_expenses']:
            desc = f" — {e['description']}" if e['description'] else ""
            acct = f" [{e['account_name']}]" if e['account_name'] else ""
            lines.append(f"  [{e['id']}] {e['date']}  {e['kind']:7s}  "
                         f"{e['amount']:>8.2f}  {e['category']}{acct}{desc}")
    return "\n".join(lines)


# ---------------- TOOL EXECUTORS (DB) ----------------

def _recalc_account_balance(account_id):
    """Recompute an account's balance from its transactions."""
    acct = db.session.get(Account, account_id)
    if not acct:
        return
    rows = (db.session.query(Expense.kind, func.sum(Expense.amount))
            .filter(Expense.account_id == account_id)
            .group_by(Expense.kind).all())
    income = 0.0
    expense = 0.0
    for kind, total in rows:
        if kind == "income":
            income = float(total)
        elif kind == "expense":
            expense = float(total)
    acct.balance = round(income - expense, 2)
    db.session.commit()


def _tool_create_expense(user_id, args):
    try:
        amount = float(args.get("amount"))
        if amount <= 0:
            return {"error": "amount must be positive"}
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}

    kind = args.get("kind", "expense")
    if kind not in ("expense", "income"):
        return {"error": "kind must be 'expense' or 'income'"}

    date_str = args.get("date") or date.today().isoformat()
    try:
        expense_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}

    category = (args.get("category") or "Other").strip()
    description = (args.get("description") or "").strip()

    # Optional account
    account_id = args.get("account_id")
    if account_id is not None:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            account_id = None
        if account_id:
            acct = db.session.get(Account, account_id)
            if not acct or acct.user_id != user_id:
                return {"error": f"no account with id {account_id}"}

    e = Expense(user_id=user_id, kind=kind, amount=amount,
                category=category, description=description,
                date=expense_date, account_id=account_id or None)
    db.session.add(e)
    db.session.commit()

    if e.account_id:
        _recalc_account_balance(e.account_id)

    result = {"ok": True, "id": e.id, "amount": amount, "category": category,
              "date": expense_date.isoformat(), "kind": kind}
    if account_id:
        acct = db.session.get(Account, account_id)
        result["account"] = acct.name
        result["account_new_balance"] = acct.balance
    return result


def _tool_delete_expense(user_id, args):
    eid = args.get("id")
    if eid is None:
        return {"error": "id is required"}
    try:
        eid = int(eid)
    except (TypeError, ValueError):
        return {"error": "id must be an integer"}

    e = Expense.query.filter_by(id=eid, user_id=user_id).first()
    if not e:
        return {"error": f"no expense with id {eid}"}
    info = {"id": e.id, "amount": e.amount, "category": e.category,
            "date": e.date.isoformat(), "kind": e.kind}
    account_id = e.account_id
    db.session.delete(e)
    db.session.commit()

    if account_id:
        _recalc_account_balance(account_id)
        info["account_recalculated"] = True

    return {"ok": True, "deleted": info}


def _tool_create_category(user_id, args):
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    kind = args.get("kind", "expense")
    if kind not in ("expense", "income"):
        return {"error": "kind must be 'expense' or 'income'"}

    if Category.query.filter_by(user_id=user_id, name=name).first():
        return {"error": f"category '{name}' already exists"}

    budget = args.get("monthly_budget")
    if budget is not None:
        try:
            budget = float(budget)
            if budget < 0:
                budget = None
        except (TypeError, ValueError):
            budget = None

    c = Category(user_id=user_id, name=name, kind=kind, monthly_budget=budget)
    db.session.add(c)
    db.session.commit()
    return {"ok": True, "id": c.id, "name": name, "kind": kind, "budget": budget}


def _tool_update_budget(user_id, args):
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    try:
        budget = float(args.get("monthly_budget"))
        if budget < 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "monthly_budget must be a positive number"}

    c = Category.query.filter_by(user_id=user_id, name=name).first()
    if not c:
        return {"error": f"no category named '{name}'"}

    c.monthly_budget = budget
    db.session.commit()
    return {"ok": True, "name": name, "monthly_budget": budget}


def _tool_create_goal(user_id, args):
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    try:
        target = float(args.get("target_amount"))
        if target <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "target_amount must be positive"}

    current = 0.0
    if args.get("current_amount") is not None:
        try:
            current = float(args["current_amount"])
            if current < 0:
                current = 0.0
        except (TypeError, ValueError):
            current = 0.0

    deadline = None
    if args.get("deadline"):
        try:
            deadline = datetime.strptime(args["deadline"], "%Y-%m-%d").date()
        except ValueError:
            return {"error": "deadline must be YYYY-MM-DD"}

    g = SavingsGoal(user_id=user_id, name=name, target_amount=target,
                    current_amount=current, deadline=deadline)
    db.session.add(g)
    db.session.commit()
    return {"ok": True, "id": g.id, "name": name, "target": target,
            "current": current, "deadline": deadline.isoformat() if deadline else None}


def _tool_deposit_goal(user_id, args):
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    try:
        amount = float(args.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "amount must be positive"}

    g = SavingsGoal.query.filter_by(user_id=user_id, name=name).first()
    if not g:
        return {"error": f"no savings goal named '{name}'"}

    g.current_amount += amount
    db.session.commit()
    return {"ok": True, "name": name, "deposited": amount,
            "new_total": g.current_amount, "target": g.target_amount}


# ---------------- ACCOUNT TOOLS ----------------

def _tool_create_account(user_id, args):
    """Create a new account."""
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}

    kind = (args.get("kind") or "bank").lower()
    valid_kinds = {"bank", "savings", "cash", "card", "wallet", "investment", "other"}
    if kind not in valid_kinds:
        return {"error": f"invalid kind '{kind}'. Must be one of: {', '.join(valid_kinds)}"}

    try:
        balance = float(args.get("balance", 0))
    except (TypeError, ValueError):
        balance = 0.0

    currency = (args.get("currency") or "USD").upper()
    if currency not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported currency '{currency}'"}

    color = (args.get("color") or "#4f46e5").strip()
    notes = (args.get("notes") or "").strip()

    # Prevent duplicate names
    if Account.query.filter_by(user_id=user_id, name=name).first():
        return {"error": f"account '{name}' already exists"}

    a = Account(user_id=user_id, name=name, kind=kind, balance=balance,
                currency=currency, color=color, notes=notes)
    db.session.add(a)
    db.session.commit()
    return {"ok": True, "id": a.id, "name": name, "kind": kind,
            "balance": balance, "currency": currency}


def _tool_update_account(user_id, args):
    """Update an existing account's name, balance, kind, etc."""
    acct_id = args.get("account_id")
    if acct_id is None:
        return {"error": "account_id is required"}
    try:
        acct_id = int(acct_id)
    except (TypeError, ValueError):
        return {"error": "account_id must be an integer"}

    acct = Account.query.filter_by(id=acct_id, user_id=user_id).first()
    if not acct:
        return {"error": f"no account with id {acct_id}"}

    changes = {}

    if "name" in args and args["name"]:
        new_name = args["name"].strip()
        # check no duplicate
        existing = Account.query.filter_by(user_id=user_id, name=new_name).first()
        if existing and existing.id != acct.id:
            return {"error": f"account '{new_name}' already exists"}
        acct.name = new_name
        changes["name"] = new_name

    if "kind" in args and args["kind"]:
        kind = args["kind"].lower()
        valid_kinds = {"bank", "savings", "cash", "card", "wallet", "investment", "other"}
        if kind not in valid_kinds:
            return {"error": f"invalid kind '{kind}'"}
        acct.kind = kind
        changes["kind"] = kind

    if "balance" in args and args["balance"] is not None:
        try:
            acct.balance = float(args["balance"])
            changes["balance"] = acct.balance
        except (TypeError, ValueError):
            return {"error": "balance must be a number"}

    if "currency" in args and args["currency"]:
        currency = args["currency"].upper()
        if currency not in SUPPORTED_CURRENCIES:
            return {"error": f"unsupported currency '{currency}'"}
        acct.currency = currency
        changes["currency"] = currency

    if "color" in args and args["color"]:
        acct.color = args["color"].strip()
        changes["color"] = acct.color

    if "notes" in args and args["notes"] is not None:
        acct.notes = args["notes"].strip()
        changes["notes"] = acct.notes

    if "archived" in args:
        acct.archived = bool(args["archived"])
        changes["archived"] = acct.archived

    if not changes:
        return {"error": "no changes provided"}

    db.session.commit()
    return {"ok": True, "id": acct.id, "name": acct.name, "changes": changes}


def _tool_delete_account(user_id, args):
    """Delete an account. Transactions are kept but unlinked."""
    acct_id = args.get("account_id")
    if acct_id is None:
        return {"error": "account_id is required"}
    try:
        acct_id = int(acct_id)
    except (TypeError, ValueError):
        return {"error": "account_id must be an integer"}

    acct = Account.query.filter_by(id=acct_id, user_id=user_id).first()
    if not acct:
        return {"error": f"no account with id {acct_id}"}

    name = acct.name
    Expense.query.filter_by(account_id=acct.id).update({"account_id": None})
    db.session.delete(acct)
    db.session.commit()
    return {"ok": True, "deleted": {"id": acct_id, "name": name},
            "note": "its transactions were kept but unlinked"}


def _tool_transfer_between_accounts(user_id, args):
    """Move money between two accounts."""
    try:
        from_id = int(args.get("from_account_id"))
        to_id = int(args.get("to_account_id"))
    except (TypeError, ValueError):
        return {"error": "from_account_id and to_account_id must be integers"}

    if from_id == to_id:
        return {"error": "cannot transfer to the same account"}

    try:
        amount = float(args.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return {"error": "amount must be a positive number"}

    src = Account.query.filter_by(id=from_id, user_id=user_id).first()
    dst = Account.query.filter_by(id=to_id, user_id=user_id).first()
    if not src:
        return {"error": f"no source account with id {from_id}"}
    if not dst:
        return {"error": f"no destination account with id {to_id}"}

    date_str = args.get("date") or date.today().isoformat()
    try:
        tdate = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return {"error": "date must be YYYY-MM-DD"}

    description = (args.get("description") or "").strip() or "Transfer"

    db.session.add(Expense(
        user_id=user_id, kind="expense", amount=amount, category="Transfer",
        description=f"→ {dst.name}: {description}",
        date=tdate, account_id=src.id,
    ))
    db.session.add(Expense(
        user_id=user_id, kind="income", amount=amount, category="Transfer",
        description=f"← {src.name}: {description}",
        date=tdate, account_id=dst.id,
    ))
    db.session.commit()

    _recalc_account_balance(src.id)
    _recalc_account_balance(dst.id)

    src = db.session.get(Account, src.id)
    dst = db.session.get(Account, dst.id)
    return {
        "ok": True,
        "amount": amount,
        "from": {"name": src.name, "new_balance": src.balance},
        "to": {"name": dst.name, "new_balance": dst.balance},
    }


# ---------------- EXCHANGE TOOLS ----------------

def _tool_get_exchange_rate(user_id, args):
    from_c = (args.get("from_currency") or "USD").upper()
    to_c = (args.get("to_currency") or "USD").upper()
    if from_c not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported from_currency '{from_c}'"}
    if to_c not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported to_currency '{to_c}'"}
    if from_c == to_c:
        return {"ok": True, "rate": 1.0, "from": from_c, "to": to_c,
                "description": f"1 {from_c} = 1 {to_c}"}
    converted, rate = convert_currency(1.0, from_c, to_c)
    if rate is None:
        return {"error": "could not fetch rate"}
    return {"ok": True, "from": from_c, "to": to_c, "rate": rate,
            "description": f"1 {from_c} = {rate} {to_c}"}


def _tool_convert_currency(user_id, args):
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    from_c = (args.get("from_currency") or "USD").upper()
    to_c = (args.get("to_currency") or "USD").upper()
    if from_c not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported from_currency '{from_c}'"}
    if to_c not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported to_currency '{to_c}'"}
    converted, rate = convert_currency(amount, from_c, to_c)
    if converted is None:
        return {"error": "could not fetch rate"}
    return {"ok": True, "amount": amount, "from": from_c, "to": to_c,
            "rate": rate, "converted": converted}


def _tool_get_rate_board(user_id, args):
    from models import User
    user = db.session.get(User, user_id)
    base = (args.get("base_currency") or (user.base_currency if user else "USD") or "USD").upper()
    if base not in SUPPORTED_CURRENCIES:
        return {"error": f"unsupported base_currency '{base}'"}
    preferred = ["EUR", "GBP", "CNY", "NGN", "JPY", "INR", "CAD", "AUD", "CHF", "ZAR", "KES", "GHS"]
    codes = [c for c in preferred if c in SUPPORTED_CURRENCIES and c != base]
    for c in SUPPORTED_CURRENCIES:
        if c not in codes and c != base:
            codes.append(c)
    board = build_rate_board(base, codes)
    if not board:
        return {"error": "could not fetch rates"}
    return {"ok": True, "base": base, "count": len(board),
            "rates": [{"code": r["code"], "name": r["name"],
                       "symbol": r["symbol"], "rate": r["rate"]} for r in board]}


TOOL_EXECUTORS = {
    "create_expense": _tool_create_expense,
    "delete_expense": _tool_delete_expense,
    "create_category": _tool_create_category,
    "update_budget": _tool_update_budget,
    "create_goal": _tool_create_goal,
    "deposit_goal": _tool_deposit_goal,
    "create_account": _tool_create_account,
    "update_account": _tool_update_account,
    "delete_account": _tool_delete_account,
    "transfer_between_accounts": _tool_transfer_between_accounts,
    "get_exchange_rate": _tool_get_exchange_rate,
    "convert_currency": _tool_convert_currency,
    "get_rate_board": _tool_get_rate_board,
}


# ---------------- SYSTEM PROMPT ----------------

SYSTEM_PROMPT = """You are Ledger's AI financial advisor. You can READ and MODIFY the user's data, and look up live exchange rates.

Available actions:

EXPENSES
- create_expense (add expense/income; optionally linked to an account_id)
- delete_expense

CATEGORIES
- create_category
- update_budget

SAVINGS GOALS
- create_goal
- deposit_goal

ACCOUNTS (bank, savings, cash, credit card, wallet, investment)
- create_account
- update_account (change name, balance, kind, currency, color, notes, archive)
- delete_account (transactions are kept but unlinked)
- transfer_between_accounts

EXCHANGE RATES
- get_exchange_rate
- convert_currency
- get_rate_board

RULES:
1. When the user asks to make a change (add/delete/update/create/transfer), USE THE APPROPRIATE TOOL.
2. Accounts have IDs shown in the ACCOUNTS section. Reference them by ID when calling account tools.
3. When adding an expense that mentions an account (e.g. "from my GTBank"), pass the account_id.
4. When transferring, always specify both from_account_id and to_account_id.
5. Reply in the same language the user wrote in.
6. Confirm actions with real numbers after success.
7. If a tool fails, explain clearly.
8. Never invent IDs.
9. Be warm and concise. 2-3 short paragraphs max."""


# ---------------- TOOL DECLARATIONS ----------------

TOOL_DECLARATIONS = [{
    "name": "create_expense",
    "description": "Add a new expense or income entry, optionally linked to an account.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["expense", "income"]},
            "amount": {"type": "number"},
            "category": {"type": "string"},
            "description": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "account_id": {"type": "integer", "description": "Optional account ID"},
        },
        "required": ["amount", "category", "date"],
    },
}, {
    "name": "delete_expense",
    "description": "Delete an expense by its ID.",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "integer"}},
        "required": ["id"],
    },
}, {
    "name": "create_category",
    "description": "Create a new category.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["expense", "income"]},
            "monthly_budget": {"type": "number"},
        },
        "required": ["name", "kind"],
    },
}, {
    "name": "update_budget",
    "description": "Set the monthly budget for an existing category.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "monthly_budget": {"type": "number"},
        },
        "required": ["name", "monthly_budget"],
    },
}, {
    "name": "create_goal",
    "description": "Create a new savings goal.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "target_amount": {"type": "number"},
            "current_amount": {"type": "number"},
            "deadline": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["name", "target_amount"],
    },
}, {
    "name": "deposit_goal",
    "description": "Deposit money into an existing savings goal.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "amount": {"type": "number"},
        },
        "required": ["name", "amount"],
    },
}, {
    "name": "create_account",
    "description": "Create a new financial account (bank, savings, cash, credit card, wallet, investment, or other).",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["bank", "savings", "cash", "card", "wallet", "investment", "other"]},
            "balance": {"type": "number", "description": "Starting balance"},
            "currency": {"type": "string", "description": "3-letter code, e.g. USD"},
            "color": {"type": "string", "description": "Hex color, e.g. #4f46e5"},
            "notes": {"type": "string"},
        },
        "required": ["name"],
    },
}, {
    "name": "update_account",
    "description": "Update an existing account — rename, change balance, kind, currency, color, notes, or archive it.",
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "integer"},
            "name": {"type": "string"},
            "kind": {"type": "string", "enum": ["bank", "savings", "cash", "card", "wallet", "investment", "other"]},
            "balance": {"type": "number"},
            "currency": {"type": "string"},
            "color": {"type": "string"},
            "notes": {"type": "string"},
            "archived": {"type": "boolean"},
        },
        "required": ["account_id"],
    },
}, {
    "name": "delete_account",
    "description": "Delete an account. Its transactions will be kept but unlinked from the account.",
    "parameters": {
        "type": "object",
        "properties": {"account_id": {"type": "integer"}},
        "required": ["account_id"],
    },
}, {
    "name": "transfer_between_accounts",
    "description": "Move money from one account to another.",
    "parameters": {
        "type": "object",
        "properties": {
            "from_account_id": {"type": "integer"},
            "to_account_id": {"type": "integer"},
            "amount": {"type": "number"},
            "description": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["from_account_id", "to_account_id", "amount"],
    },
}, {
    "name": "get_exchange_rate",
    "description": "Get the current live exchange rate between two currencies.",
    "parameters": {
        "type": "object",
        "properties": {
            "from_currency": {"type": "string"},
            "to_currency": {"type": "string"},
        },
        "required": ["from_currency", "to_currency"],
    },
}, {
    "name": "convert_currency",
    "description": "Convert an amount between currencies using live rates.",
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {"type": "number"},
            "from_currency": {"type": "string"},
            "to_currency": {"type": "string"},
        },
        "required": ["amount", "from_currency", "to_currency"],
    },
}, {
    "name": "get_rate_board",
    "description": "Get a table of many currency rates against a base currency.",
    "parameters": {
        "type": "object",
        "properties": {"base_currency": {"type": "string"}},
    },
}]


# ---------------- GEMINI ----------------

GEMINI_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-flash-latest",
    "gemini-pro-latest",
]


def _try_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, []

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, []

    client = genai.Client(api_key=api_key)
    tool = types.Tool(function_declarations=TOOL_DECLARATIONS)
    config = types.GenerateContentConfig(tools=[tool])

    response = None
    for model in GEMINI_MODELS:
        try:
            response = client.models.generate_content(
                model=model, contents=prompt, config=config,
            )
            break
        except Exception as e:
            err = str(e)
            if "API_KEY_INVALID" in err or "API key not valid" in err:
                return None, []
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                continue
            if "503" in err or "UNAVAILABLE" in err:
                time.sleep(2)
                continue
            continue

    if response is None:
        return None, []

    tool_calls = []
    final_text = ""
    try:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "function_call") and part.function_call:
                tool_calls.append({"name": part.function_call.name,
                                   "args": dict(part.function_call.args)})
            elif hasattr(part, "text") and part.text:
                final_text += part.text
    except (AttributeError, IndexError):
        pass

    return final_text, tool_calls


# ---------------- OPENAI-COMPATIBLE ----------------

OPENAI_PROVIDERS = [
    {
        "name": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "api_key_env": "CEREBRAS_API_KEY",
        "models": ["gpt-oss-120b", "qwen-3.8-27b"],
    },
    {
        "name": "Mistral",
        "base_url": "https://api.mistral.ai/v1",
        "api_key_env": "MISTRAL_API_KEY",
        "models": [
            "mistral-medium-latest",
            "mistral-small-latest",
            "ministral-14b-latest",
            "ministral-8b-latest",
        ],
    },
]


def _openai_tool_schema():
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": d["parameters"],
            },
        }
        for d in TOOL_DECLARATIONS
    ]


def _try_openai_provider(provider, system_prompt, user_message, history_messages):
    api_key = os.environ.get(provider["api_key_env"])
    if not api_key:
        return None, []

    try:
        from openai import OpenAI
    except ImportError:
        return None, []

    client = OpenAI(base_url=provider["base_url"], api_key=api_key)
    tools = _openai_tool_schema()

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_messages)
    messages.append({"role": "user", "content": user_message})

    response = None
    for model in provider["models"]:
        try:
            response = client.chat.completions.create(
                model=model, messages=messages,
                tools=tools, tool_choice="auto", timeout=30,
            )
            break
        except Exception as e:
            err = str(e)
            if "429" in err or "rate" in err.lower() or "quota" in err.lower():
                continue
            if "model" in err.lower() and ("not" in err.lower() or "invalid" in err.lower()):
                continue
            continue

    if response is None:
        return None, []

    choice = response.choices[0].message
    tool_calls = []
    if choice.tool_calls:
        for tc in choice.tool_calls:
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except (ValueError, AttributeError):
                args = {}
            tool_calls.append({"name": tc.function.name, "args": args})

    return (choice.content or ""), tool_calls


# ---------------- MAIN CHAT ----------------

def chat(user_id, user_message):
    snapshot = build_financial_snapshot(user_id)
    data_block = format_snapshot_for_prompt(snapshot)

    history_rows = (AiChat.query.filter_by(user_id=user_id)
                    .order_by(AiChat.created_at.desc()).limit(10).all())
    history = list(reversed(history_rows))

    history_text = "\n".join([f"{h.role}: {h.content}" for h in history]) or "(no prior conversation)"
    full_prompt = f"""{SYSTEM_PROMPT}

=== USER'S FINANCIAL DATA ===
{data_block}

=== CONVERSATION HISTORY ===
{history_text}

=== USER'S MESSAGE ===
{user_message}

Respond in the user's language. Call tools if needed."""

    openai_history = [{"role": h.role, "content": h.content} for h in history]
    openai_user_msg = f"""Financial data:
{data_block}

User message: {user_message}"""

    final_text, tool_calls = _try_gemini(full_prompt)
    used_provider = "Gemini" if (final_text is not None or tool_calls) else None

    if final_text is None and not tool_calls:
        for provider in OPENAI_PROVIDERS:
            text, calls = _try_openai_provider(
                provider, SYSTEM_PROMPT, openai_user_msg, openai_history,
            )
            if text is not None or calls:
                final_text = text
                tool_calls = calls
                used_provider = provider["name"]
                break

    if final_text is None and not tool_calls:
        return ("The AI advisor is temporarily unavailable. "
                "All providers have hit their limits. Please try again later.")

    print(f"[AI] Responded via {used_provider}")

    if not tool_calls:
        reply = final_text or "(no response)"
        db.session.add(AiChat(user_id=user_id, role="user", content=user_message))
        db.session.add(AiChat(user_id=user_id, role="assistant", content=reply))
        db.session.commit()
        return reply

    tool_results_text = []
    for call in tool_calls:
        fname = call["name"]
        args = call["args"]
        executor = TOOL_EXECUTORS.get(fname)

        if not executor:
            result = {"error": f"unknown tool: {fname}"}
        else:
            try:
                result = executor(user_id, args)
            except Exception as e:
                result = {"error": str(e)}

        tool_results_text.append(f"[{fname}] args={json.dumps(args)} -> {json.dumps(result)}")

    followup_prompt = f"""You just executed:
{chr(10).join(tool_results_text)}

Write a short, friendly response to the user IN THEIR LANGUAGE. Cite real numbers."""

    followup_text, _ = _try_gemini(followup_prompt)

    if not followup_text:
        for provider in OPENAI_PROVIDERS:
            text, _ = _try_openai_provider(provider, SYSTEM_PROMPT, followup_prompt, [])
            if text:
                followup_text = text
                break

    reply = followup_text or f"Done: {tool_results_text}"

    db.session.add(AiChat(user_id=user_id, role="user", content=user_message))
    db.session.add(AiChat(user_id=user_id, role="assistant", content=reply))
    db.session.commit()
    return reply