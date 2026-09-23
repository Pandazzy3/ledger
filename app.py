import os
import smtplib
import ssl
from email.message import EmailMessage

from datetime import date, datetime, timedelta
from calendar import monthrange
from io import StringIO
import csv

from dotenv import load_dotenv
from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    jsonify, Response, abort, g,
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func

from models import (
    db, User, Category, Expense, RecurringRule, SavingsGoal, AlertLog, ApiToken,
)
from forms import (
    RegisterForm, LoginForm, ExpenseForm, CategoryForm,
    RecurringForm, GoalForm, SettingsForm,
)
from api_auth import require_token

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-fallback-key")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ledger.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["WTF_CSRF_ENABLED"] = True

db.init_app(app)

from flask_wtf.csrf import CSRFProtect
csrf = CSRFProtect()
csrf.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access that page."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------- EMAIL ----------------

def send_alert_email(to_email, username, category, spent, budget, threshold):
    subject = f"⚠️ Ledger alert: {category} budget at {threshold}%"
    body = (
        f"Hi {username},\n\n"
        f"Heads up — your spending in '{category}' has reached {spent:.2f}, "
        f"which is {threshold}% or more of your {budget:.2f} monthly budget.\n\n"
        f"Log in to Ledger to review your spending.\n\n— Ledger"
    )
    smtp_host = os.environ.get("SMTP_HOST")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")

    if not all([smtp_host, smtp_user, smtp_pass]):
        print(f"[ALERT - no SMTP configured] {to_email}: {subject}")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = to_email
    msg.set_content(body)
    context = ssl.create_default_context()
    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        print(f"[ALERT SENT] {to_email}: {subject}")
    except Exception as e:
        print(f"[ALERT FAILED] {e}")


def check_budget_alerts(user):
    if not user.email_alerts:
        return
    today = date.today()
    month_start = today.replace(day=1)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])
    current_month = today.strftime("%Y-%m")

    cats = Category.query.filter_by(user_id=user.id, kind="expense").all()
    if not cats:
        return
    spent_rows = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(
            Expense.user_id == user.id, Expense.kind == "expense",
            Expense.date >= month_start, Expense.date <= month_end,
        )
        .group_by(Expense.category).all()
    )
    spent_map = {r[0]: float(r[1]) for r in spent_rows}
    threshold = user.alert_threshold or 80

    for c in cats:
        if not c.monthly_budget or c.monthly_budget <= 0:
            continue
        spent = spent_map.get(c.name, 0.0)
        pct = spent / c.monthly_budget * 100.0
        if pct < threshold:
            continue
        already = AlertLog.query.filter_by(
            user_id=user.id, category=c.name,
            month=current_month, threshold=threshold,
        ).first()
        if already:
            continue
        send_alert_email(user.email, user.username, c.name, spent, c.monthly_budget, threshold)
        db.session.add(AlertLog(
            user_id=user.id, category=c.name,
            month=current_month, threshold=threshold,
        ))
        db.session.commit()


# ---------------- HELPERS ----------------

def parse_filters():
    today = date.today()
    default_start = today - timedelta(days=29)
    default_end = today
    start_str = request.args.get("start", default_start.isoformat())
    end_str = request.args.get("end", default_end.isoformat())
    category = request.args.get("category", "").strip()
    kind = request.args.get("kind", "").strip()
    q = request.args.get("q", "").strip()
    try:
        start = datetime.strptime(start_str, "%Y-%m-%d").date()
    except ValueError:
        start = default_start; start_str = default_start.isoformat()
    try:
        end = datetime.strptime(end_str, "%Y-%m-%d").date()
    except ValueError:
        end = default_end; end_str = default_end.isoformat()
    if kind not in ("expense", "income"):
        kind = ""
    return {
        "start": start, "end": end, "start_str": start_str, "end_str": end_str,
        "category": category, "kind": kind, "q": q,
    }


def apply_filters(query, user_id, filters):
    q = query.filter(
        Expense.user_id == user_id,
        Expense.date >= filters["start"],
        Expense.date <= filters["end"],
    )
    if filters["category"]:
        q = q.filter(Expense.category == filters["category"])
    if filters["kind"]:
        q = q.filter(Expense.kind == filters["kind"])
    if filters["q"]:
        q = q.filter(Expense.description.ilike(f"%{filters['q']}%"))
    return q


def run_recurring_for_user(user_id):
    today = date.today()
    current_month = today.strftime("%Y-%m")
    rules = RecurringRule.query.filter_by(user_id=user_id, active=True).all()
    posted = 0
    for rule in rules:
        if rule.last_posted_month == current_month:
            continue
        if today.day < rule.day_of_month:
            continue
        day = min(rule.day_of_month, today.day)
        existing = Expense.query.filter_by(
            user_id=user_id, recurring_id=rule.id,
        ).filter(func.strftime("%Y-%m", Expense.date) == current_month).first()
        if existing:
            rule.last_posted_month = current_month
            db.session.commit()
            continue
        db.session.add(Expense(
            user_id=user_id, kind=rule.kind, amount=rule.amount,
            category=rule.category,
            description=rule.description or f"[Auto] {rule.category}",
            date=today.replace(day=day), recurring_id=rule.id,
        ))
        rule.last_posted_month = current_month
        posted += 1
    if posted:
        db.session.commit()
    return posted


def build_summary(user_id, filters):
    total_income = (
        apply_filters(db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)), user_id, filters)
        .filter(Expense.kind == "income").scalar()
    )
    total_expense = (
        apply_filters(db.session.query(func.coalesce(func.sum(Expense.amount), 0.0)), user_id, filters)
        .filter(Expense.kind == "expense").scalar()
    )
    net = total_income - total_expense
    by_cat = (
        apply_filters(db.session.query(Expense.category, func.sum(Expense.amount)), user_id, filters)
        .filter(Expense.kind == "expense")
        .group_by(Expense.category)
        .order_by(func.sum(Expense.amount).desc()).all()
    )
    pie_labels = [r[0] for r in by_cat]
    pie_values = [round(float(r[1]), 2) for r in by_cat]
    start = filters["start"]; end = filters["end"]
    span_days = max(1, (end - start).days + 1)
    daily = (
        apply_filters(db.session.query(Expense.date, func.sum(Expense.amount)), user_id, filters)
        .filter(Expense.kind == "expense").group_by(Expense.date).order_by(Expense.date).all()
    )
    daily_map = {r[0]: float(r[1]) for r in daily}
    if span_days > 90:
        cap_start = end - timedelta(days=89)
        bar_labels, bar_values = [], []
        for i in range(90):
            d = cap_start + timedelta(days=i)
            bar_labels.append(d.strftime("%m-%d"))
            bar_values.append(round(daily_map.get(d, 0.0), 2))
    else:
        bar_labels, bar_values = [], []
        for i in range(span_days):
            d = start + timedelta(days=i)
            bar_labels.append(d.strftime("%m-%d"))
            bar_values.append(round(daily_map.get(d, 0.0), 2))
    total_entries = apply_filters(db.session.query(func.count(Expense.id)), user_id, filters).scalar() or 0
    avg_expense = (
        apply_filters(db.session.query(func.avg(Expense.amount)), user_id, filters)
        .filter(Expense.kind == "expense").scalar()
    )
    biggest = (
        apply_filters(db.session.query(func.max(Expense.amount)), user_id, filters)
        .filter(Expense.kind == "expense").scalar()
    )
    today = date.today()
    month_start = today.replace(day=1)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])
    categories = Category.query.filter_by(user_id=user_id).order_by(Category.name).all()
    spent_rows = (
        db.session.query(Expense.category, func.sum(Expense.amount))
        .filter(
            Expense.user_id == user_id, Expense.kind == "expense",
            Expense.date >= month_start, Expense.date <= month_end,
        )
        .group_by(Expense.category).all()
    )
    spent_map = {r[0]: float(r[1]) for r in spent_rows}
    budget_progress = []
    for c in categories:
        if c.kind != "expense":
            continue
        spent = spent_map.get(c.name, 0.0)
        if c.monthly_budget and c.monthly_budget > 0:
            pct = min(100.0, spent / c.monthly_budget * 100.0)
            status = "over" if spent > c.monthly_budget else ("warn" if pct >= 80 else "ok")
            budget_progress.append({
                "name": c.name, "budget": c.monthly_budget, "spent": round(spent, 2),
                "pct": round(pct, 1), "remaining": round(c.monthly_budget - spent, 2),
                "over": status == "over", "status": status,
            })
        else:
            budget_progress.append({
                "name": c.name, "budget": None, "spent": round(spent, 2),
                "pct": 0, "remaining": None, "over": False, "status": "no-budget",
            })
    return {
        "total_income": round(float(total_income), 2),
        "total_expense": round(float(total_expense), 2),
        "net": round(float(net), 2),
        "pie": {"labels": pie_labels, "values": pie_values},
        "bar": {"labels": bar_labels, "values": bar_values},
        "budget_progress": budget_progress,
        "month_label": today.strftime("%B %Y"),
        "quick": {
            "count": int(total_entries),
            "avg_expense": round(float(avg_expense), 2) if avg_expense else 0.0,
            "biggest": round(float(biggest), 2) if biggest else 0.0,
        },
    }


# ---------------- PUBLIC ----------------

@app.route("/")
def index():
    return render_template("index.html", user_count=User.query.count())


@app.route("/api")
def api_docs():
    return render_template("api_docs.html")


# ---------------- AUTH ----------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(username=form.username.data).first():
            flash("That username is already taken.", "danger")
        elif User.query.filter_by(email=form.email.data.lower()).first():
            flash("That email is already registered.", "danger")
        else:
            u = User(
                username=form.username.data.strip(),
                email=form.email.data.strip().lower(),
                password_hash=generate_password_hash(form.password.data),
            )
            db.session.add(u)
            db.session.commit()
            defaults = [
                ("Food", "expense", None), ("Rent", "expense", None),
                ("Transport", "expense", None), ("Utilities", "expense", None),
                ("Entertainment", "expense", None), ("Health", "expense", None),
                ("Salary", "income", None), ("Other", "expense", None),
            ]
            for name, kind, budget in defaults:
                db.session.add(Category(user_id=u.id, name=name, kind=kind, monthly_budget=budget))
            db.session.commit()
            flash("Account created! You can now log in.", "success")
            return redirect(url_for("login"))
    return render_template("register.html", form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and check_password_hash(user.password_hash, form.password.data):
            login_user(user)
            run_recurring_for_user(user.id)
            check_budget_alerts(user)
            flash(f"Welcome back, {user.username}!", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html", form=form)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("index"))


# ---------------- DASHBOARD ----------------

@app.route("/dashboard")
@login_required
def dashboard():
    run_recurring_for_user(current_user.id)
    check_budget_alerts(current_user)
    filters = parse_filters()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    PER_PAGE = 10
    base_query = apply_filters(db.session.query(Expense), current_user.id, filters)
    total_entries = base_query.count()
    total_pages = max(1, (total_entries + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages
    entries = (
        base_query.order_by(Expense.date.desc(), Expense.id.desc())
        .offset((page - 1) * PER_PAGE).limit(PER_PAGE).all()
    )
    summary = build_summary(current_user.id, filters)
    all_categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    goals = SavingsGoal.query.filter_by(user_id=current_user.id).order_by(SavingsGoal.created_at).all()
    return render_template(
        "dashboard.html",
        expenses=entries, summary=summary, filters=filters,
        all_categories=all_categories, page=page,
        total_pages=total_pages, total_entries=total_entries,
        goals=goals,
    )


# ---------------- CSV EXPORT ----------------

@app.route("/export.csv")
@login_required
def export_csv():
    filters = parse_filters()
    entries = (
        apply_filters(db.session.query(Expense), current_user.id, filters)
        .order_by(Expense.date.desc(), Expense.id.desc()).all()
    )
    buf = StringIO()
    buf.write("\ufeff")  # UTF-8 BOM for Excel
    writer = csv.writer(buf)
    writer.writerow(["Date", "Type", "Category", "Description", "Amount"])
    for e in entries:
        writer.writerow([
            e.date.isoformat(), e.kind, e.category,
            e.description or "", f"{e.amount:.2f}",
        ])
    filename = f"ledger_{filters['start_str']}_to_{filters['end_str']}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------- EXPENSES ----------------

@app.route("/expenses/add", methods=["GET", "POST"])
@login_required
def add_expense():
    form = ExpenseForm()
    if form.validate_on_submit():
        db.session.add(Expense(
            user_id=current_user.id, kind=form.kind.data,
            amount=form.amount.data,
            category=form.category.data.strip() or "Other",
            description=(form.description.data or "").strip(),
            date=form.date.data,
        ))
        db.session.commit()
        check_budget_alerts(current_user)
        flash(f"{form.kind.data.capitalize()} of {form.amount.data:.2f} added.", "success")
        return redirect(url_for("dashboard"))
    if not form.date.data:
        form.date.data = date.today()
    return render_template("add_expense.html", form=form)


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
@login_required
def edit_expense(expense_id):
    expense = db.session.get(Expense, expense_id)
    if not expense or expense.user_id != current_user.id:
        flash("Entry not found.", "danger")
        return redirect(url_for("dashboard"))
    form = ExpenseForm(obj=expense)
    if form.validate_on_submit():
        expense.kind = form.kind.data
        expense.amount = form.amount.data
        expense.category = form.category.data.strip() or "Other"
        expense.description = (form.description.data or "").strip()
        expense.date = form.date.data
        db.session.commit()
        check_budget_alerts(current_user)
        flash("Entry updated.", "success")
        return redirect(url_for("dashboard"))
    return render_template("edit_expense.html", form=form, expense=expense)


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    expense = db.session.get(Expense, expense_id)
    if not expense or expense.user_id != current_user.id:
        flash("Entry not found.", "danger")
        return redirect(url_for("dashboard"))
    db.session.delete(expense)
    db.session.commit()
    flash("Entry deleted.", "info")
    return redirect(url_for("dashboard"))


# ---------------- CATEGORIES ----------------

@app.route("/categories")
@login_required
def list_categories():
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    form = CategoryForm()
    return render_template("categories.html", categories=categories, form=form)


@app.route("/categories/add", methods=["POST"])
@login_required
def add_category():
    form = CategoryForm()
    if form.validate_on_submit():
        if Category.query.filter_by(user_id=current_user.id, name=form.name.data.strip()).first():
            flash(f"Category '{form.name.data}' already exists.", "danger")
        else:
            db.session.add(Category(
                user_id=current_user.id, name=form.name.data.strip(),
                kind=form.kind.data, monthly_budget=form.monthly_budget.data,
            ))
            db.session.commit()
            flash(f"Category '{form.name.data}' added.", "success")
    else:
        flash("Invalid category details.", "danger")
    return redirect(url_for("list_categories"))


@app.route("/categories/<int:cat_id>/update", methods=["POST"])
@login_required
def update_category(cat_id):
    cat = db.session.get(Category, cat_id)
    if not cat or cat.user_id != current_user.id:
        flash("Category not found.", "danger")
        return redirect(url_for("list_categories"))
    form = CategoryForm()
    if form.validate_on_submit():
        cat.name = form.name.data.strip()
        cat.kind = form.kind.data
        cat.monthly_budget = form.monthly_budget.data
        db.session.commit()
        flash(f"Category '{cat.name}' updated.", "success")
    return redirect(url_for("list_categories"))


@app.route("/categories/<int:cat_id>/delete", methods=["POST"])
@login_required
def delete_category(cat_id):
    cat = db.session.get(Category, cat_id)
    if not cat or cat.user_id != current_user.id:
        flash("Category not found.", "danger")
        return redirect(url_for("list_categories"))
    db.session.delete(cat)
    db.session.commit()
    flash(f"Category '{cat.name}' deleted.", "info")
    return redirect(url_for("list_categories"))


# ---------------- RECURRING ----------------

@app.route("/recurring")
@login_required
def list_recurring():
    rules = RecurringRule.query.filter_by(user_id=current_user.id).order_by(RecurringRule.created_at).all()
    categories = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    form = RecurringForm()
    return render_template("recurring.html", rules=rules, categories=categories, form=form)


@app.route("/recurring/add", methods=["POST"])
@login_required
def add_recurring():
    form = RecurringForm()
    if form.validate_on_submit():
        db.session.add(RecurringRule(
            user_id=current_user.id, kind=form.kind.data, amount=form.amount.data,
            category=form.category.data.strip() or "Other",
            description=(form.description.data or "").strip(),
            day_of_month=form.day_of_month.data,
        ))
        db.session.commit()
        flash("Recurring rule added.", "success")
    else:
        flash("Invalid recurring rule.", "danger")
    return redirect(url_for("list_recurring"))


@app.route("/recurring/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle_recurring(rule_id):
    rule = db.session.get(RecurringRule, rule_id)
    if not rule or rule.user_id != current_user.id:
        flash("Rule not found.", "danger")
        return redirect(url_for("list_recurring"))
    rule.active = not rule.active
    db.session.commit()
    flash(f"Rule {'activated' if rule.active else 'paused'}.", "info")
    return redirect(url_for("list_recurring"))


@app.route("/recurring/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete_recurring(rule_id):
    rule = db.session.get(RecurringRule, rule_id)
    if not rule or rule.user_id != current_user.id:
        flash("Rule not found.", "danger")
        return redirect(url_for("list_recurring"))
    db.session.delete(rule)
    db.session.commit()
    flash("Rule deleted.", "info")
    return redirect(url_for("list_recurring"))


# ---------------- GOALS ----------------

@app.route("/goals")
@login_required
def list_goals():
    goals = SavingsGoal.query.filter_by(user_id=current_user.id).order_by(SavingsGoal.created_at).all()
    form = GoalForm()
    return render_template("goals.html", goals=goals, form=form)


@app.route("/goals/add", methods=["POST"])
@login_required
def add_goal():
    form = GoalForm()
    if form.validate_on_submit():
        db.session.add(SavingsGoal(
            user_id=current_user.id, name=form.name.data.strip(),
            target_amount=form.target_amount.data,
            current_amount=form.current_amount.data or 0.0,
            deadline=form.deadline.data,
        ))
        db.session.commit()
        flash(f"Goal '{form.name.data}' added.", "success")
    else:
        flash("Invalid goal details.", "danger")
    return redirect(url_for("list_goals"))


@app.route("/goals/<int:goal_id>/deposit", methods=["POST"])
@login_required
def deposit_goal(goal_id):
    goal = db.session.get(SavingsGoal, goal_id)
    if not goal or goal.user_id != current_user.id:
        flash("Goal not found.", "danger")
        return redirect(url_for("list_goals"))
    amount_raw = request.form.get("amount", "").strip()
    try:
        amount = float(amount_raw)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Deposit amount must be positive.", "danger")
        return redirect(url_for("list_goals"))
    goal.current_amount += amount
    db.session.commit()
    flash(f"Deposited {amount:.2f} to '{goal.name}'.", "success")
    return redirect(url_for("list_goals"))


@app.route("/goals/<int:goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    goal = db.session.get(SavingsGoal, goal_id)
    if not goal or goal.user_id != current_user.id:
        flash("Goal not found.", "danger")
        return redirect(url_for("list_goals"))
    db.session.delete(goal)
    db.session.commit()
    flash("Goal deleted.", "info")
    return redirect(url_for("list_goals"))


# ---------------- REPORT ----------------

@app.route("/report")
@login_required
def report():
    today = date.today()
    this_start = today.replace(day=1)
    this_end = today.replace(day=monthrange(today.year, today.month)[1])
    prev_month_end = this_start - timedelta(days=1)
    prev_start = prev_month_end.replace(day=1)

    def totals(start, end):
        inc = (
            db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
            .filter(Expense.user_id == current_user.id, Expense.kind == "income",
                    Expense.date >= start, Expense.date <= end).scalar() or 0.0)
        exp = (
            db.session.query(func.coalesce(func.sum(Expense.amount), 0.0))
            .filter(Expense.user_id == current_user.id, Expense.kind == "expense",
                    Expense.date >= start, Expense.date <= end).scalar() or 0.0)
        return round(float(inc), 2), round(float(exp), 2)

    this_inc, this_exp = totals(this_start, this_end)
    prev_inc, prev_exp = totals(prev_start, prev_month_end)

    def by_category(start, end):
        rows = (
            db.session.query(Expense.category, func.sum(Expense.amount))
            .filter(Expense.user_id == current_user.id, Expense.kind == "expense",
                    Expense.date >= start, Expense.date <= end)
            .group_by(Expense.category).order_by(func.sum(Expense.amount).desc()).all()
        )
        return [(r[0], round(float(r[1]), 2)) for r in rows]

    this_cats = by_category(this_start, this_end)
    prev_cats = by_category(prev_start, prev_month_end)
    prev_map = dict(prev_cats); this_map = dict(this_cats)
    all_names = sorted(set(this_map) | set(prev_map))
    comparison = []
    for name in all_names:
        t = this_map.get(name, 0.0); p = prev_map.get(name, 0.0)
        delta = round(t - p, 2)
        pct = round((delta / p * 100.0), 1) if p > 0 else None
        comparison.append({"name": name, "this": t, "prev": p, "delta": delta, "pct": pct})
    comparison.sort(key=lambda r: r["this"], reverse=True)

    return render_template(
        "report.html",
        this_label=this_start.strftime("%B %Y"),
        prev_label=prev_start.strftime("%B %Y"),
        this_inc=this_inc, this_exp=this_exp, this_net=round(this_inc - this_exp, 2),
        prev_inc=prev_inc, prev_exp=prev_exp, prev_net=round(prev_inc - prev_exp, 2),
        comparison=comparison,
    )


# ---------------- SETTINGS ----------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = SettingsForm(obj=current_user)
    if form.validate_on_submit():
        current_user.email_alerts = form.email_alerts.data
        current_user.alert_threshold = int(form.alert_threshold.data)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings"))
    form.alert_threshold.data = str(current_user.alert_threshold or 80)
    return render_template("settings.html", form=form)


# ---------------- API TOKENS (web UI) ----------------

@app.route("/api/tokens", methods=["GET", "POST"])
@login_required
def api_tokens():
    if request.method == "POST":
        name = request.form.get("name", "").strip() or "Unnamed"
        token = ApiToken(user_id=current_user.id, token=ApiToken.generate(), name=name)
        db.session.add(token)
        db.session.commit()
        flash(f"Token '{name}' created. Copy it now — you won't see it again.", "success")
        return redirect(url_for("api_tokens"))
    tokens = ApiToken.query.filter_by(user_id=current_user.id).order_by(ApiToken.created_at.desc()).all()
    return render_template("api_tokens.html", tokens=tokens)


@app.route("/api/tokens/<int:token_id>/delete", methods=["POST"])
@login_required
def delete_api_token(token_id):
    t = db.session.get(ApiToken, token_id)
    if not t or t.user_id != current_user.id:
        flash("Token not found.", "danger")
        return redirect(url_for("api_tokens"))
    db.session.delete(t)
    db.session.commit()
    flash("Token revoked.", "info")
    return redirect(url_for("api_tokens"))


# ---------------- REST API v1 ----------------

@app.route("/api/v1/me", methods=["GET"])
@require_token
def api_me():
    u = g.current_user
    return jsonify({
        "username": u.username,
        "email": u.email,
        "email_alerts": u.email_alerts,
        "alert_threshold": u.alert_threshold,
    })


@app.route("/api/v1/expenses", methods=["GET", "POST"])
@require_token
def api_expenses():
    user = g.current_user
    if request.method == "GET":
        query = Expense.query.filter_by(user_id=user.id)
        start = request.args.get("start")
        end = request.args.get("end")
        if start:
            try: query = query.filter(Expense.date >= datetime.strptime(start, "%Y-%m-%d").date())
            except ValueError: pass
        if end:
            try: query = query.filter(Expense.date <= datetime.strptime(end, "%Y-%m-%d").date())
            except ValueError: pass
        cat = request.args.get("category")
        if cat: query = query.filter(Expense.category == cat)
        kind = request.args.get("kind")
        if kind in ("expense", "income"): query = query.filter(Expense.kind == kind)
        rows = query.order_by(Expense.date.desc(), Expense.id.desc()).limit(500).all()
        return jsonify([e.to_dict() for e in rows])

    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a positive number"}), 400
    try:
        expense_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    kind = data.get("kind", "expense")
    if kind not in ("expense", "income"):
        return jsonify({"error": "kind must be expense or income"}), 400

    e = Expense(
        user_id=user.id, kind=kind, amount=amount,
        category=(data.get("category") or "Other").strip(),
        description=(data.get("description") or "").strip(),
        date=expense_date,
    )
    db.session.add(e)
    db.session.commit()
    check_budget_alerts(user)
    return jsonify(e.to_dict()), 201


@app.route("/api/v1/expenses/<int:expense_id>", methods=["GET", "PUT", "DELETE"])
@require_token
def api_expense_detail(expense_id):
    user = g.current_user
    e = db.session.get(Expense, expense_id)
    if not e or e.user_id != user.id:
        return jsonify({"error": "not found"}), 404

    if request.method == "GET":
        return jsonify(e.to_dict())

    if request.method == "DELETE":
        db.session.delete(e)
        db.session.commit()
        return jsonify({"deleted": True, "id": expense_id})

    data = request.get_json(silent=True) or {}
    if "kind" in data:
        if data["kind"] not in ("expense", "income"):
            return jsonify({"error": "invalid kind"}), 400
        e.kind = data["kind"]
    if "amount" in data:
        try:
            e.amount = float(data["amount"])
            if e.amount <= 0: raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "invalid amount"}), 400
    if "category" in data:
        e.category = (data["category"] or "Other").strip()
    if "description" in data:
        e.description = (data["description"] or "").strip()
    if "date" in data:
        try:
            e.date = datetime.strptime(data["date"], "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "invalid date"}), 400
    db.session.commit()
    return jsonify(e.to_dict())


@app.route("/api/v1/categories", methods=["GET"])
@require_token
def api_categories():
    rows = Category.query.filter_by(user_id=g.current_user.id).order_by(Category.name).all()
    return jsonify([c.to_dict() for c in rows])


@app.route("/api/v1/summary", methods=["GET"])
@require_token
def api_summary():
    filters = parse_filters()
    return jsonify(build_summary(g.current_user.id, filters))


# ---------------- ERROR HANDLERS ----------------

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    if request.path.startswith("/api/"):
        return jsonify({"error": "internal server error"}), 500
    return render_template("500.html"), 500


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=True)