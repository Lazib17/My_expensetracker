import json
import os
import secrets
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import date, datetime, date as dt_date
from pathlib import Path

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, inspect, text
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


BASE_DIR = Path(__file__).resolve().parent
PRESET_CATEGORIES = ["Food", "Transport", "Rent", "Utilities", "Health"]
OTHER_CATEGORY = "Others"
CATEGORIES = PRESET_CATEGORIES + [OTHER_CATEGORY]


def database_uri() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://") :]
        elif url.startswith("postgresql://") and "+psycopg2" not in url:
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url

    if os.environ.get("VERCEL"):
        db_path = Path(tempfile.gettempdir()) / "expenses.db"
    else:
        instance_dir = BASE_DIR / "instance"
        instance_dir.mkdir(parents=True, exist_ok=True)
        db_path = instance_dir / "expenses.db"
    return "sqlite:///" + db_path.resolve().as_posix()


app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config["SQLALCHEMY_DATABASE_URI"] = database_uri()
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "my-secret-key")
app.config["GOOGLE_CLIENT_ID"] = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()
app.config["GOOGLE_CLIENT_SECRET"] = (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()

if os.environ.get("VERCEL"):
    app.config["PREFERRED_URL_SCHEME"] = "https"
    app.config["SESSION_COOKIE_SECURE"] = True
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please sign in to save and view your expenses."
login_manager.login_message_category = "error"

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    google_id = db.Column(db.String(128), unique=True, nullable=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    picture = db.Column(db.String(1000))
    password_hash = db.Column(db.String(255), nullable=True)
    budget_target = db.Column(db.Float, nullable=True)
    expenses = db.relationship("Expense", backref="user", lazy=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.email}>"


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    date = db.Column(db.Date, nullable=False, default=datetime.today)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    def __repr__(self):
        return f"<Expense {self.id} - {self.category}: {self.amount}>"


def migrate_schema():
    inspector = inspect(db.engine)
    tables = inspector.get_table_names()
    if "expense" in tables:
        columns = {column["name"] for column in inspector.get_columns("expense")}
        if "user_id" not in columns:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE expense ADD COLUMN user_id INTEGER"))
    if "users" in tables:
        columns = {column["name"] for column in inspector.get_columns("users")}
        if "password_hash" not in columns:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))


def init_db():
    db.create_all()
    migrate_schema()


with app.app_context():
    init_db()


@app.before_request
def _ensure_db():
    if not app.config.get("DB_READY"):
        init_db()
        app.config["DB_READY"] = True


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def google_enabled() -> bool:
    return bool(app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"])


def google_authorize_url(redirect_uri: str) -> str:
    state = secrets.token_urlsafe(24)
    session["oauth_state"] = state
    params = {
        "client_id": app.config["GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def google_fetch_user(code: str, redirect_uri: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": app.config["GOOGLE_CLIENT_ID"],
            "client_secret": app.config["GOOGLE_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode()
    token_req = urllib.request.Request(GOOGLE_TOKEN_URL, data=payload, method="POST")
    with urllib.request.urlopen(token_req, timeout=15) as resp:
        token = json.loads(resp.read().decode())

    access_token = token.get("access_token")
    if not access_token:
        raise RuntimeError("Google did not return an access token.")

    info_req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(info_req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@")[-1]


def parse_date_or_none(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def current_month_bounds(today=None):
    today = today or date.today()
    start = today.replace(day=1)
    end = today.replace(day=monthrange(today.year, today.month)[1])
    return start, end


def month_spend(user_id, start, end):
    total = (
        db.session.query(func.coalesce(func.sum(Expense.amount), 0))
        .filter(Expense.user_id == user_id, Expense.date >= start, Expense.date <= end)
        .scalar()
    )
    return round(float(total or 0), 2)


def budget_status(user):
    start, end = current_month_bounds()
    spent = month_spend(user.id, start, end)
    target = user.budget_target
    if not target or target <= 0:
        return {
            "target": None,
            "spent": spent,
            "remaining": None,
            "percent": 0,
            "over": False,
            "warning": False,
            "month_label": start.strftime("%B %Y"),
        }

    percent = int(round((spent / target) * 100))
    remaining = round(target - spent, 2)
    return {
        "target": target,
        "spent": spent,
        "remaining": remaining,
        "percent": percent,
        "over": spent > target,
        "warning": target * 0.8 <= spent <= target,
        "month_label": start.strftime("%B %Y"),
    }


def notify_budget(user):
    status = budget_status(user)
    if status["over"]:
        over_by = abs(status["remaining"])
        flash(
            f"Budget alert: you have exceeded your {status['month_label']} target "
            f"of ${status['target']:.2f} by ${over_by:.2f}.",
            "error",
        )
    elif status["warning"]:
        flash(
            f"Heads up: you have used {status['percent']}% of your "
            f"{status['month_label']} budget.",
            "warning",
        )
    return status


def resolve_category(form) -> str:
    selected = (form.get("category") or "").strip()
    if selected != OTHER_CATEGORY:
        return selected
    return (form.get("custom_category") or "").strip()[:50]


def user_filter_categories(user_id):
    extras = [
        name
        for (name,) in (
            db.session.query(Expense.category)
            .filter(Expense.user_id == user_id, ~Expense.category.in_(PRESET_CATEGORIES))
            .distinct()
            .order_by(Expense.category)
            .all()
        )
        if name
    ]
    return PRESET_CATEGORIES + extras


def filtered_expense_query(user_id, start_date=None, end_date=None, category=""):
    query = Expense.query.filter(Expense.user_id == user_id)
    if start_date:
        query = query.filter(Expense.date >= start_date)
    if end_date:
        query = query.filter(Expense.date <= end_date)
    if category:
        query = query.filter(Expense.category == category)
    return query


def get_user_expense(expense_id):
    return Expense.query.filter_by(id=expense_id, user_id=current_user.id).first_or_404()


def claim_orphan_expenses(user):
    if User.query.count() != 1:
        return
    Expense.query.filter(Expense.user_id.is_(None)).update({"user_id": user.id})
    db.session.commit()


def upsert_google_user(info):
    email = (info.get("email") or "").strip().lower()
    google_id = info.get("sub")
    name = (info.get("name") or "").strip() or email.split("@")[0]
    picture = info.get("picture")

    user = None
    if google_id:
        user = User.query.filter_by(google_id=google_id).first()
    if user is None and email:
        user = User.query.filter_by(email=email).first()

    if user is None:
        user = User(google_id=google_id, email=email, name=name, picture=picture)
        db.session.add(user)
        db.session.commit()
        claim_orphan_expenses(user)
        return user

    user.google_id = user.google_id or google_id
    user.email = email or user.email
    user.name = name or user.name
    user.picture = picture or user.picture
    db.session.commit()
    return user


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("signup.html", google_enabled=google_enabled())

    name = (request.form.get("name") or "").strip()
    email = normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    if not name or not is_valid_email(email) or not password:
        flash("Please fill in your name, email, and password.", "error")
        return redirect(url_for("signup"))
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("signup"))
    if password != confirm:
        flash("Passwords do not match.", "error")
        return redirect(url_for("signup"))

    user = User.query.filter_by(email=email).first()
    if user and user.password_hash:
        flash("That email already has an account. Please sign in.", "error")
        return redirect(url_for("login"))

    if user is None:
        user = User(email=email, name=name)
        db.session.add(user)
        db.session.flush()
        claim_orphan_expenses(user)
    else:
        user.name = name or user.name

    user.set_password(password)
    db.session.commit()
    login_user(user)
    flash(f"Account created for {user.email}.", "success")
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html", google_enabled=google_enabled())

    email = normalize_email(request.form.get("email"))
    password = request.form.get("password") or ""
    user = User.query.filter_by(email=email).first() if is_valid_email(email) else None

    if user is None or not user.check_password(password):
        if user and not user.password_hash:
            flash("This account uses Google sign-in. Use Continue with Google, or create a password on the sign-up page.", "error")
        else:
            flash("Incorrect email or password.", "error")
        return redirect(url_for("login"))

    login_user(user)
    flash(f"Signed in as {user.email}", "success")
    return redirect(url_for("index"))


@app.route("/login/google")
def login_google():
    if not google_enabled():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))
    redirect_uri = url_for("google_callback", _external=True)
    return redirect(google_authorize_url(redirect_uri))


@app.route("/login/google/callback")
def google_callback():
    if not google_enabled():
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))

    if request.args.get("state") != session.pop("oauth_state", None):
        flash("Google sign-in could not be verified. Please try again.", "error")
        return redirect(url_for("login"))

    code = request.args.get("code")
    if not code:
        flash("Google sign-in was cancelled.", "error")
        return redirect(url_for("login"))

    try:
        info = google_fetch_user(code, url_for("google_callback", _external=True))
    except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError):
        flash("Google sign-in failed. Please try again.", "error")
        return redirect(url_for("login"))

    if not (info.get("email") or "").strip():
        flash("Google did not return an email address.", "error")
        return redirect(url_for("login"))

    user = upsert_google_user(info)
    login_user(user)
    flash(f"Signed in as {user.email}", "success")
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    start_str = (request.args.get("start") or "").strip()
    end_str = (request.args.get("end") or "").strip()
    selected_category = (request.args.get("category") or "").strip()

    start_date = parse_date_or_none(start_str)
    end_date = parse_date_or_none(end_str)

    if start_date and end_date and end_date < start_date:
        flash("End date cannot be earlier than start date", "error")
        start_date = end_date = None
        start_str = end_str = ""

    query = filtered_expense_query(current_user.id, start_date, end_date, selected_category)
    expenses = query.order_by(Expense.date.desc(), Expense.id.desc()).all()
    total = round(sum(e.amount for e in expenses), 2)

    cat_q = db.session.query(Expense.category, func.sum(Expense.amount)).filter(Expense.user_id == current_user.id)
    day_q = db.session.query(Expense.date, func.sum(Expense.amount)).filter(Expense.user_id == current_user.id)

    if start_date:
        cat_q = cat_q.filter(Expense.date >= start_date)
        day_q = day_q.filter(Expense.date >= start_date)
    if end_date:
        cat_q = cat_q.filter(Expense.date <= end_date)
        day_q = day_q.filter(Expense.date <= end_date)
    if selected_category:
        cat_q = cat_q.filter(Expense.category == selected_category)
        day_q = day_q.filter(Expense.category == selected_category)

    cat_rows = cat_q.group_by(Expense.category).all()
    cat_labels = [c for c, _ in cat_rows]
    cat_values = [round(float(s or 0), 2) for _, s in cat_rows]

    day_rows = day_q.group_by(Expense.date).order_by(Expense.date).all()
    day_labels = [d.isoformat() for d, _ in day_rows]
    day_values = [round(float(s or 0), 2) for _, s in day_rows]

    return render_template(
        "index.html",
        catergories=CATEGORIES,
        filter_categories=user_filter_categories(current_user.id),
        today=date.today().isoformat(),
        expenses=expenses,
        total=total,
        start_str=start_str,
        end_str=end_str,
        selected_category=selected_category,
        cat_labels=cat_labels,
        cat_values=cat_values,
        day_labels=day_labels,
        day_values=day_values,
        budget=budget_status(current_user),
    )


@app.route("/budget", methods=["POST"])
@login_required
def set_budget():
    raw = (request.form.get("budget_target") or "").strip()
    if not raw:
        current_user.budget_target = None
        db.session.commit()
        flash("Budget target cleared.", "success")
        return redirect(url_for("index"))

    try:
        value = float(raw)
        if value <= 0:
            raise ValueError
    except ValueError:
        flash("Budget target must be a positive number.", "error")
        return redirect(url_for("index"))

    current_user.budget_target = round(value, 2)
    db.session.commit()
    flash("Budget target saved.", "success")
    notify_budget(current_user)
    return redirect(url_for("index"))


@app.route("/add", methods=["POST"])
@login_required
def add():
    description = (request.form.get("description") or "").strip()
    amount_str = (request.form.get("amount") or "").strip()
    category = resolve_category(request.form)
    date_str = (request.form.get("date") or "").strip()

    if not description or not amount_str or not category:
        flash("Please fill description, amount and category", "error")
        return redirect(url_for("index"))

    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Amount must be a positive number", "error")
        return redirect(url_for("index"))

    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except ValueError:
        d = date.today()

    expense = Expense(
        description=description,
        amount=amount,
        category=category,
        date=d,
        user_id=current_user.id,
    )
    db.session.add(expense)
    db.session.commit()
    flash("Expense added", "success")
    notify_budget(current_user)
    return redirect(url_for("index"))


@app.route("/delete/<int:expense_id>", methods=["POST"])
@login_required
def delete(expense_id):
    expense = get_user_expense(expense_id)
    db.session.delete(expense)
    db.session.commit()
    flash("Expense deleted", "success")
    return redirect(url_for("index"))


@app.route("/edit/<int:expense_id>", methods=["GET"])
@login_required
def edit(expense_id):
    expense = get_user_expense(expense_id)
    is_custom = expense.category not in PRESET_CATEGORIES
    return render_template(
        "edit.html",
        expense=expense,
        categories=CATEGORIES,
        selected_category="Others" if is_custom else expense.category,
        custom_category=expense.category if is_custom else "",
        today=dt_date.today().isoformat(),
    )


@app.route("/edit/<int:expense_id>", methods=["POST"])
@login_required
def edit_post(expense_id):
    expense = get_user_expense(expense_id)

    description = (request.form.get("description") or "").strip()
    amount_str = (request.form.get("amount") or "").strip()
    category = resolve_category(request.form)
    date_str = (request.form.get("date") or "").strip()

    if not description or not amount_str or not category:
        flash("Please fill description, amount and category", "error")
        return redirect(url_for("edit", expense_id=expense_id))

    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Amount must be a positive number", "error")
        return redirect(url_for("edit", expense_id=expense_id))

    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else dt_date.today()
    except ValueError:
        d = dt_date.today()

    expense.description = description
    expense.amount = amount
    expense.category = category
    expense.date = d
    db.session.commit()
    flash("Expense updated", "success")
    notify_budget(current_user)
    return redirect(url_for("index"))


@app.route("/export.csv")
@login_required
def export_csv():
    start_str = (request.args.get("start") or "").strip()
    end_str = (request.args.get("end") or "").strip()
    selected_category = (request.args.get("category") or "").strip()

    start_date = parse_date_or_none(start_str)
    end_date = parse_date_or_none(end_str)
    expenses = (
        filtered_expense_query(current_user.id, start_date, end_date, selected_category)
        .order_by(Expense.date, Expense.id)
        .all()
    )

    lines = ["date, description, category, amount"]
    for expense in expenses:
        lines.append(f"{expense.date.isoformat()}, {expense.description}, {expense.category}, {expense.amount:.2f}")

    fname_start = start_str or "all"
    fname_end = end_str or "all"
    return Response(
        "\n".join(lines),
        headers={
            "Content-Type": "text/csv",
            "Content-Disposition": f"attachment; filename=expenses_{fname_start}_to_{fname_end}.csv",
        },
    )


if __name__ == "__main__":
    app.run(debug=True, port=4848)
