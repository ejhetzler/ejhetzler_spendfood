from flask import Flask, render_template, request, redirect, session
import csv
import os
import sqlite3
from functools import wraps
from datetime import datetime
from collections import Counter, defaultdict
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SESSION_SECRET', 'dev-fallback-secret')

HEADERS   = ["date", "item", "meal", "source", "amount", "reference"]
DB_FILE   = "users.db"
DATA_DIR  = "data"

CATEGORY_MAP = {
    "Breakfast":          ("Breakfast", "Eating Out"),
    "Lunch":              ("Lunch",     "Eating Out"),
    "Dinner":             ("Dinner",    "Eating Out"),
    "Snack":              ("Snack",     "Eating Out"),
    "Drink":              ("Drink",     "Eating Out"),
    "Groceries":          ("",          "Grocery Shopping"),
    "Home Cooked":        ("",          "From Groceries"),
    "Groceries (Buying)": ("",          "Grocery Shopping"),
    "Groceries (Eating)": ("",          "From Groceries"),
}

# ── DB SETUP ────────────────────────────────────────────────────────────────

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            password   TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

init_db()


def user_file(user_id):
    return os.path.join(DATA_DIR, f"expenses_{user_id}.csv")


def ensure_user_file(user_id):
    path = user_file(user_id)
    if not os.path.exists(path):
        with open(path, mode="w", newline="") as f:
            csv.writer(f).writerow(HEADERS)
    return path


# ── AUTH HELPERS ─────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


@app.context_processor
def inject_user():
    return {
        "current_username": session.get("username"),
        "logged_in": "user_id" in session,
    }


# ── DATA HELPERS ─────────────────────────────────────────────────────────────

def migrate_file_if_needed(path):
    if not os.path.exists(path):
        return
    with open(path, mode="r") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        if "category" in fields and "meal" not in fields:
            rows = list(reader)
        else:
            return
    new_rows = []
    for row in rows:
        meal, source = CATEGORY_MAP.get(row.get("category", ""), ("", "Eating Out"))
        new_rows.append({
            "date": row["date"], "item": row["item"],
            "meal": meal, "source": source,
            "amount": row["amount"], "reference": row.get("reference", ""),
        })
    with open(path, mode="w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(new_rows)


def read_expenses(user_id):
    path = ensure_user_file(user_id)
    migrate_file_if_needed(path)
    expenses = []
    with open(path, mode="r") as f:
        for row in csv.DictReader(f):
            row["amount"] = float(row["amount"])
            row.setdefault("meal", "")
            row.setdefault("source", "Eating Out")
            row.setdefault("reference", "")
            expenses.append(row)
    return expenses


def is_spending(e):
    return e["source"] != "From Groceries"


# ── AUTH ROUTES ──────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect("/")
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if user and check_password_hash(user["password"], password):
            session["user_id"]  = user["id"]
            session["username"] = user["username"]
            ensure_user_file(user["id"])
            return redirect("/")
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect("/")
    error = None
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        if len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        else:
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.execute(
                    "INSERT INTO users (username, password, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), datetime.today().isoformat())
                )
                conn.commit()
                user_id = conn.execute(
                    "SELECT id FROM users WHERE username = ?", (username,)
                ).fetchone()[0]
                conn.close()
                session["user_id"]  = user_id
                session["username"] = username
                ensure_user_file(user_id)
                return redirect("/")
            except sqlite3.IntegrityError:
                error = "That username is already taken — please choose another."
    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/landing")


# ── APP ROUTES ───────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    uid = session["user_id"]
    expenses = read_expenses(uid)
    today = datetime.today().strftime("%Y-%m-%d")
    today_total  = sum(e["amount"] for e in expenses if e["date"] == today and is_spending(e))
    grocery_runs = [e for e in expenses if e["source"] == "Grocery Shopping"]
    recent = list(reversed(expenses))[:10]
    return render_template(
        "index.html",
        expenses=recent,
        today_total=round(today_total, 2),
        today=today,
        grocery_runs=grocery_runs,
    )


@app.route("/add", methods=["POST"])
@login_required
def add_expense():
    uid       = session["user_id"]
    date      = request.form["date"]
    item      = request.form["item"]
    meal      = request.form.get("meal", "")
    source    = request.form["source"]
    reference = request.form.get("reference", "")
    amount    = "0.00" if source == "From Groceries" else request.form.get("amount", "0.00")
    path = ensure_user_file(uid)
    with open(path, mode="a", newline="") as f:
        csv.writer(f).writerow([date, item, meal, source, amount, reference])
    return redirect("/")


@app.route("/history")
@login_required
def history():
    expenses = list(reversed(read_expenses(session["user_id"])))
    return render_template("history.html", expenses=expenses)


@app.route("/stats")
@login_required
def stats():
    expenses = read_expenses(session["user_id"])
    spending = [e for e in expenses if is_spending(e)]

    monthly = defaultdict(float)
    for e in spending:
        monthly[e["date"][:7]] += e["amount"]
    all_months     = sorted(monthly.keys())[-6:]
    monthly_values = [round(monthly[m], 2) for m in all_months]

    meal_totals = defaultdict(float)
    for e in spending:
        if e["source"] != "Grocery Shopping" and e["meal"]:
            meal_totals[e["meal"]] += e["amount"]
    meal_totals = dict(sorted(meal_totals.items(), key=lambda x: -x[1]))

    source_totals = defaultdict(float)
    for e in spending:
        source_totals[e["source"]] += e["amount"]
    source_totals = dict(sorted(source_totals.items(), key=lambda x: -x[1]))

    item_counter   = Counter(e["item"] for e in spending)
    top_items      = item_counter.most_common(5)
    total_spent    = round(sum(e["amount"] for e in spending), 2)
    this_month_key = datetime.today().strftime("%Y-%m")
    this_month     = round(sum(e["amount"] for e in spending if e["date"][:7] == this_month_key), 2)
    biggest        = max(spending, key=lambda x: x["amount"], default=None)

    return render_template(
        "stats.html",
        monthly_labels=all_months,
        monthly_values=monthly_values,
        meal_totals=meal_totals,
        source_totals=source_totals,
        top_items=top_items,
        total_spent=total_spent,
        this_month=this_month,
        biggest=biggest,
        total_entries=len(expenses),
    )


@app.route("/landing")
def landing():
    return render_template("landing.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
