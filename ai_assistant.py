"""AI budget assistant powered by Google Gemini with function calling."""
import os
import time
import json
from datetime import date, datetime, timedelta
from calendar import monthrange

from sqlalchemy import func

from models import db, Expense, Category, SavingsGoal, AiChat
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
                    "description": e.description or ""} for e in recent]

    base_currency = user.base_currency if user else "USD"
    display_currency = user.currency if user else "USD"
    language = user.language if user else "en"

    return {
        "today": today.isoformat(),
        "base_currency": base_currency,
        "display_currency": display_currency,
        "language": language,
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
            lines.append(f"  [{e['id']}] {e['date']}  {e['kind']:7s}  "
                         f"{e['amount']:>8.2f}  {e['category']}{desc}")
    return "\n".join(lines)


# ---------------- TOOL EXECUTORS (DB) ----------------

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

    e = Expense(user_id=user_id, kind=kind, amount=amount,
                category=category, description=description, date=expense_date)
    db.session.add(e)
    db.session.commit()
    return {"ok": True, "id": e.id, "amount": amount, "category": category,
            "date": expense_date.isoformat(), "kind": kind}


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
    db.session.delete(e)
    db.session.commit()
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


# ---------------- TOOL EXECUTORS (exchange) ----------------

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

    return {
        "ok": True, "from": from_c, "to": to_c, "rate": rate,
        "description": f"1 {from_c} = {rate} {to_c}",
    }


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

    return {
        "ok": True, "base": base, "count": len(board),
        "rates": [
            {"code": r["code"], "name": r["name"], "symbol": r["symbol"], "rate": r["rate"]}
            for r in board
        ],
    }


TOOL_EXECUTORS = {
    "create_expense": _tool_create_expense,
    "delete_expense": _tool_delete_expense,
    "create_category": _tool_create_category,
    "update_budget": _tool_update_budget,
    "create_goal": _tool_create_goal,
    "deposit_goal": _tool_deposit_goal,
    "get_exchange_rate": _tool_get_exchange_rate,
    "convert_currency": _tool_convert_currency,
    "get_rate_board": _tool_get_rate_board,
}


# ---------------- SYSTEM PROMPT ----------------

SYSTEM_PROMPT = """You are Ledger's AI financial advisor. You can READ the user's data, MODIFY it, and look up live exchange rates.

You can:
- Analyze spending, budgets, and goals (from the data provided)
- Add expenses and income (create_expense)
- Delete expenses (delete_expense)
- Create categories (create_category)
- Update a category's monthly budget (update_budget)
- Create savings goals (create_goal)
- Deposit into savings goals (deposit_goal)
- Get a single exchange rate (get_exchange_rate) — e.g. "USD to NGN"
- Convert an amount between currencies (convert_currency) — e.g. "convert $250 to EUR"
- Get the full rate board for the user's base currency (get_rate_board) — e.g. "show all rates"

RULES:
1. When the user asks to make a change (add/delete/update/create), USE THE APPROPRIATE TOOL.
2. When the user asks about exchange rates or currency conversion, USE the rate tools.
3. IMPORTANT: Reply in the same language the user wrote in. If the user's UI language is 'zh', reply in Chinese. If 'es', reply in Spanish. Etc.
4. After calling a tool successfully, confirm with the real numbers.
5. If a tool fails, explain clearly and suggest a fix.
6. Never invent IDs or rates — always use data from the tools or the snapshot.
7. If the user's intent is unclear, ask ONE clarifying question.
8. Be warm and concise. 2-3 short paragraphs maximum.

Today's date and the user's base/display currencies are provided in the data block."""


# ---------------- MODEL SELECTION ----------------

# Preferred model names, in order. Many have separate daily quotas.
# We try them in order, falling back when one is exhausted (429).
_PREFERRED_MODELS = [
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


def _pick_model(client):
    """Pick the first preferred model that exists."""
    try:
        available = set()
        for m in client.models.list():
            name = m.name.split("/", 1)[-1] if "/" in m.name else m.name
            available.add(name)
    except Exception:
        return _PREFERRED_MODELS[0]

    for want in _PREFERRED_MODELS:
        if want in available:
            return want
    for name in sorted(available):
        if "flash" in name.lower() and "vision" not in name.lower():
            return name
    return None


# ---------------- TOOL DECLARATIONS ----------------

TOOL_DECLARATIONS = [{
    "name": "create_expense",
    "description": "Add a new expense or income entry.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["expense", "income"]},
            "amount": {"type": "number"},
            "category": {"type": "string"},
            "description": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
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
    "name": "get_exchange_rate",
    "description": "Get the current live exchange rate between two currencies.",
    "parameters": {
        "type": "object",
        "properties": {
            "from_currency": {"type": "string", "description": "3-letter code, e.g. USD"},
            "to_currency": {"type": "string", "description": "3-letter code, e.g. NGN"},
        },
        "required": ["from_currency", "to_currency"],
    },
}, {
    "name": "convert_currency",
    "description": "Convert an amount from one currency to another using live rates.",
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
    "description": "Get a table of many currency rates against a base currency. Use when the user asks for many rates at once.",
    "parameters": {
        "type": "object",
        "properties": {
            "base_currency": {"type": "string", "description": "3-letter code; defaults to user's stored currency"},
        },
    },
}]


# ---------------- CHAT ----------------

def chat(user_id, user_message):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "The AI advisor is not configured. Please set GEMINI_API_KEY."

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return "The google-genai package is not installed. Run: pip install google-genai"

    client = genai.Client(api_key=api_key)
    model_name = _pick_model(client)
    if not model_name:
        return "No Gemini model is available for this API key."

    snapshot = build_financial_snapshot(user_id)
    data_block = format_snapshot_for_prompt(snapshot)

    history_rows = (AiChat.query.filter_by(user_id=user_id)
                    .order_by(AiChat.created_at.desc()).limit(10).all())
    history = list(reversed(history_rows))
    history_text = "\n".join([f"{h.role}: {h.content}" for h in history]) or "(no prior conversation)"

    prompt = f"""{SYSTEM_PROMPT}

=== USER'S FINANCIAL DATA ===
{data_block}

=== CONVERSATION HISTORY ===
{history_text}

=== USER'S MESSAGE ===
{user_message}

Respond to the user in their language. If a database change or exchange lookup is needed, call the appropriate tool. Otherwise just answer."""

    tool = types.Tool(function_declarations=TOOL_DECLARATIONS)
    config = types.GenerateContentConfig(tools=[tool])

    # Try each candidate model until one succeeds
    candidate_models = [model_name]
    for fallback in _PREFERRED_MODELS:
        if fallback not in candidate_models:
            candidate_models.append(fallback)

    response = None
    last_error = None

    for candidate in candidate_models:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=candidate,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as e:
                last_error = e
                err = str(e)
                # 503 = temporary overload; retry same model
                if "503" in err or "UNAVAILABLE" in err or "overloaded" in err.lower():
                    time.sleep(2 * (attempt + 1))
                    continue
                # 429 = quota exhausted; try next model
                if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                    break
                # Any other error — move to next model too
                break
        if response is not None:
            break

    if response is None:
        return (f"The AI advisor hit an error. All models tried. "
                f"Last error: {last_error}")

    # Parse the response for tool calls vs. plain text
    tool_calls = []
    final_text = ""
    try:
        for part in response.candidates[0].content.parts:
            if hasattr(part, "function_call") and part.function_call:
                tool_calls.append(part.function_call)
            elif hasattr(part, "text") and part.text:
                final_text += part.text
    except (AttributeError, IndexError):
        pass

    # If no tools requested, save and return
    if not tool_calls:
        reply = final_text or "(no response)"
        db.session.add(AiChat(user_id=user_id, role="user", content=user_message))
        db.session.add(AiChat(user_id=user_id, role="assistant", content=reply))
        db.session.commit()
        return reply

    # Execute each tool call
    tool_results_text = []
    for call in tool_calls:
        fname = call.name
        args = dict(call.args) if call.args else {}
        executor = TOOL_EXECUTORS.get(fname)

        if not executor:
            result = {"error": f"unknown tool: {fname}"}
        else:
            try:
                result = executor(user_id, args)
            except Exception as e:
                result = {"error": str(e)}

        tool_results_text.append(f"[{fname}] args={json.dumps(args)} -> {json.dumps(result)}")

    # Follow-up: ask the model to summarize the results
    followup_prompt = f"""You just executed the following tool calls on the user's data:

{chr(10).join(tool_results_text)}

Write a short, friendly response to the user IN THEIR LANGUAGE. Cite real numbers and rates.
If any call returned an error, explain what went wrong and how to fix it."""

    followup = None
    for candidate in candidate_models:
        try:
            followup = client.models.generate_content(
                model=candidate,
                contents=followup_prompt,
            )
            break
        except Exception as e:
            last_error = e
            err = str(e)
            if "503" in err or "UNAVAILABLE" in err or "429" in err or "quota" in err.lower():
                continue
            break

    if followup is not None:
        try:
            reply = followup.text
        except Exception:
            reply = f"Done: {tool_results_text}"
    else:
        reply = f"Action completed (could not generate summary): {tool_results_text}"

    db.session.add(AiChat(user_id=user_id, role="user", content=user_message))
    db.session.add(AiChat(user_id=user_id, role="assistant", content=reply))
    db.session.commit()
    return reply