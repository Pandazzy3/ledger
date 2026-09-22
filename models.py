from datetime import datetime, date
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    email_alerts = db.Column(db.Boolean, default=False, nullable=False)
    alert_threshold = db.Column(db.Integer, default=80)  # 50, 80, or 100 (%)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<User {self.username}>"


class Category(db.Model):
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(50), nullable=False)
    kind = db.Column(db.String(10), nullable=False, default="expense")
    monthly_budget = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("categories", lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<Category {self.name} ({self.kind})>"


class Expense(db.Model):
    __tablename__ = "expenses"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default="expense")
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, default="Other")
    description = db.Column(db.String(200), default="")
    date = db.Column(db.Date, nullable=False)
    recurring_id = db.Column(db.Integer, db.ForeignKey("recurring_rules.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("expenses", lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<Expense {self.kind} {self.amount} {self.category}>"


class RecurringRule(db.Model):
    __tablename__ = "recurring_rules"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False, default="expense")
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False, default="Other")
    description = db.Column(db.String(200), default="")
    day_of_month = db.Column(db.Integer, nullable=False, default=1)
    active = db.Column(db.Boolean, default=True, nullable=False)
    last_posted_month = db.Column(db.String(7), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("recurring_rules", lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<Recurring {self.kind} {self.amount} {self.category} day={self.day_of_month}>"


class SavingsGoal(db.Model):
    __tablename__ = "savings_goals"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, nullable=False, default=0.0)
    deadline = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("savings_goals", lazy=True, cascade="all, delete-orphan"))

    @property
    def progress_pct(self):
        if not self.target_amount or self.target_amount <= 0:
            return 0.0
        return min(100.0, self.current_amount / self.target_amount * 100.0)

    @property
    def remaining(self):
        return max(0.0, self.target_amount - self.current_amount)

    @property
    def days_left(self):
        if not self.deadline:
            return None
        return (self.deadline - date.today()).days


class AlertLog(db.Model):
    """Tracks sent alerts so we don't spam the user."""
    __tablename__ = "alert_log"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    category = db.Column(db.String(50), nullable=False)
    month = db.Column(db.String(7), nullable=False)  # e.g. "2026-09"
    threshold = db.Column(db.Integer, nullable=False)  # 80 or 100
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<AlertLog user={self.user_id} {self.category} {self.month} {self.threshold}%>"