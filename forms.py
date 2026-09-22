from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, FloatField, DateField,
    SelectField, IntegerField, BooleanField, SubmitField,
)
from wtforms.validators import DataRequired, Email, Length, EqualTo, NumberRange, Optional


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
    submit = SubmitField("Add Rule")


class GoalForm(FlaskForm):
    name = StringField("Goal name", validators=[DataRequired(), Length(max=100)])
    target_amount = FloatField("Target amount", validators=[DataRequired(), NumberRange(min=0.01)])
    current_amount = FloatField("Already saved", validators=[Optional(), NumberRange(min=0)], default=0)
    deadline = DateField("Deadline", validators=[Optional()])
    submit = SubmitField("Add Goal")


class SettingsForm(FlaskForm):
    email_alerts = BooleanField("Send email alerts when a category nears its budget")
    alert_threshold = SelectField("Alert at",
                                  choices=[("50", "50% of budget"),
                                           ("80", "80% of budget"),
                                           ("100", "100% (over budget)")])
    submit = SubmitField("Save Settings")