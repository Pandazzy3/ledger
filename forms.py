from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, FloatField, DateField,
    SelectField, IntegerField, BooleanField, SubmitField, TextAreaField,
)
from wtforms.validators import DataRequired, Email, Length, EqualTo, NumberRange, Optional

from translations import SUPPORTED_LANGUAGES, SUPPORTED_CURRENCIES


ACCOUNT_KINDS = [
    ("bank", "Bank account"),
    ("savings", "Savings"),
    ("cash", "Cash"),
    ("card", "Credit card"),
    ("wallet", "Digital wallet"),
    ("investment", "Investment"),
    ("other", "Other"),
]


class RegisterForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=50)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=120)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField("Confirm Password", validators=[
        DataRequired(), EqualTo("password", message="Passwords must match.")
    ])
    submit = SubmitField("Sign Up")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Log In")


class ExpenseForm(FlaskForm):
    kind = SelectField("Type", choices=[("expense", "Expense"), ("income", "Income")],
                       validators=[DataRequired()])
    amount = FloatField("Amount", validators=[DataRequired(), NumberRange(min=0.01)])
    category = StringField("Category", validators=[DataRequired(), Length(max=50)])
    description = StringField("Description", validators=[Optional(), Length(max=200)])
    date = DateField("Date", validators=[DataRequired()])
    account_id = SelectField("Account", coerce=int, choices=[], validators=[Optional()])
    submit = SubmitField("Save")


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=50)])
    kind = SelectField("Type", choices=[("expense", "Expense"), ("income", "Income")],
                       validators=[DataRequired()])
    monthly_budget = FloatField("Monthly budget", validators=[Optional(), NumberRange(min=0)])
    submit = SubmitField("Save")


class RecurringForm(FlaskForm):
    kind = SelectField("Type", choices=[("expense", "Expense"), ("income", "Income")],
                       validators=[DataRequired()])
    amount = FloatField("Amount", validators=[DataRequired(), NumberRange(min=0.01)])
    category = StringField("Category", validators=[DataRequired(), Length(max=50)])
    description = StringField("Description", validators=[Optional(), Length(max=200)])
    day_of_month = IntegerField("Day of month", validators=[DataRequired(), NumberRange(min=1, max=28)],
                                default=1)
    account_id = SelectField("Account", coerce=int, choices=[], validators=[Optional()])
    submit = SubmitField("Add Rule")


class GoalForm(FlaskForm):
    name = StringField("Goal name", validators=[DataRequired(), Length(max=100)])
    target_amount = FloatField("Target amount", validators=[DataRequired(), NumberRange(min=0.01)])
    current_amount = FloatField("Already saved", validators=[Optional(), NumberRange(min=0)], default=0)
    deadline = DateField("Deadline", validators=[Optional()])
    submit = SubmitField("Add Goal")


class AccountForm(FlaskForm):
    name = StringField("Account name", validators=[DataRequired(), Length(max=100)])
    kind = SelectField("Type", choices=ACCOUNT_KINDS, validators=[DataRequired()])
    balance = FloatField("Current balance", validators=[DataRequired()])
    currency = SelectField("Currency", choices=[], validators=[DataRequired()])
    color = StringField("Color", validators=[Optional(), Length(max=20)], default="#4f46e5")
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=300)])
    submit = SubmitField("Save")


class TransferForm(FlaskForm):
    from_account_id = SelectField("From", coerce=int, choices=[], validators=[DataRequired()])
    to_account_id = SelectField("To", coerce=int, choices=[], validators=[DataRequired()])
    amount = FloatField("Amount", validators=[DataRequired(), NumberRange(min=0.01)])
    description = StringField("Description", validators=[Optional(), Length(max=200)])
    date = DateField("Date", validators=[DataRequired()])
    submit = SubmitField("Transfer")


class SettingsForm(FlaskForm):
    email_alerts = BooleanField("Send email alerts when a category nears its budget")
    alert_threshold = SelectField("Alert at",
                                  choices=[("50", "50% of budget"),
                                           ("80", "80% of budget"),
                                           ("100", "100% (over budget)")])
    language = SelectField("Language", choices=[])
    currency = SelectField("Display Currency", choices=[])
    base_currency = SelectField("Amounts Stored In", choices=[])
    submit = SubmitField("Save Settings")