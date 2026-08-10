import os
import calendar
import datetime
import math
from flask import Flask, render_template, request, url_for, redirect, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, text


basedir = os.path.abspath(os.path.dirname(__file__))

db_path = os.environ.get('DATABASE_PATH', os.path.join(basedir, 'database.db'))
os.makedirs(os.path.dirname(db_path), exist_ok=True)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key')

expense_tracker = SQLAlchemy(app)

# What kind of payment a line item represents - independent of source, since one
# job can pay both tips (real hours worked) and a periodic shift-pay differential
# or salary (no new hours). Only 'tips' entries carry scheduled/extra hours, so
# hourly-rate analysis can include only the entries that actually represent hours worked.
INCOME_ENTRY_TYPES = ['tips', 'shift_pay', 'salary']
INCOME_ENTRY_TYPE_LABELS = {'tips': 'Tips', 'shift_pay': 'Shift Pay', 'salary': 'Salary'}
INCOME_SHIFTS = ['day', 'night']


class Expense(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    amount = expense_tracker.Column(expense_tracker.Float, nullable=False)
    category = expense_tracker.Column(expense_tracker.String(50), nullable=False)
    description = expense_tracker.Column(expense_tracker.String(200))
    date = expense_tracker.Column(expense_tracker.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f'<Expense {self.id} - {self.amount} - {self.category}>'


class Income(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    amount = expense_tracker.Column(expense_tracker.Float, nullable=False)
    scheduled_hours = expense_tracker.Column(expense_tracker.Float, nullable=False)
    extra_hours = expense_tracker.Column(expense_tracker.Float, nullable=False)
    source = expense_tracker.Column(expense_tracker.String(50), nullable=False)
    entry_type = expense_tracker.Column(expense_tracker.String(20), nullable=False, default='tips')
    shift = expense_tracker.Column(expense_tracker.String(10))
    description = expense_tracker.Column(expense_tracker.String(200))
    date = expense_tracker.Column(expense_tracker.DateTime, default=datetime.datetime.utcnow)

    def __repr__(self):
        return f'<Income {self.id} - {self.amount} - {self.source}>'


class IncomeSource(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    name = expense_tracker.Column(expense_tracker.String(100), unique=True, nullable=False)

    def __repr__(self):
        return f'<IncomeSource {self.name}>'


class ExpenseCategory(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    name = expense_tracker.Column(expense_tracker.String(100), unique=True, nullable=False)

    def __repr__(self):
        return f'<ExpenseCategory {self.name}>'


def _migrate_income_columns():
    """Backfill entry_type/shift onto an income table created before they existed."""
    with expense_tracker.engine.connect() as conn:
        existing_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(income)'))}
        if 'entry_type' not in existing_columns:
            conn.execute(text("ALTER TABLE income ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'tips'"))
        if 'shift' not in existing_columns:
            conn.execute(text('ALTER TABLE income ADD COLUMN shift VARCHAR(10)'))
        conn.commit()


def _seed_lookup_tables():
    """Backfill the source/category lookup tables from existing rows the first time they run."""
    if IncomeSource.query.count() == 0:
        names = {i.source for i in Income.query.all()}
        names |= {'Johnny Macs Bartending', 'Grill Marx Bartending'}
        for name in sorted(names):
            expense_tracker.session.add(IncomeSource(name=name))
    if ExpenseCategory.query.count() == 0:
        names = {e.category for e in Expense.query.all()}
        for name in sorted(names):
            expense_tracker.session.add(ExpenseCategory(name=name))
    expense_tracker.session.commit()


with app.app_context():
    expense_tracker.create_all()
    _migrate_income_columns()
    _seed_lookup_tables()

CHART_PLOT_HEIGHT = 212  # px, excludes the x-axis label band


def _nice_ceil(value):
    """Round up to a clean axis max (1/2/5/10 x a power of ten)."""
    if value <= 0:
        return 0
    magnitude = 10 ** math.floor(math.log10(value))
    residual = value / magnitude
    nice = 1 if residual <= 1 else 2 if residual <= 2 else 5 if residual <= 5 else 10
    return nice * magnitude


def _build_yearly_chart(year):
    year_start = datetime.datetime(year, 1, 1)
    next_year_start = datetime.datetime(year + 1, 1, 1)

    year_incomes = Income.query.filter(Income.date >= year_start, Income.date < next_year_start).all()
    year_expenses = Expense.query.filter(Expense.date >= year_start, Expense.date < next_year_start).all()

    monthly_income = [0.0] * 12
    monthly_expenses = [0.0] * 12
    for income in year_incomes:
        monthly_income[income.date.month - 1] += income.amount
    for expense in year_expenses:
        monthly_expenses[expense.date.month - 1] += expense.amount

    axis_max = _nice_ceil(max(monthly_income + monthly_expenses, default=0))

    def bar_height(value):
        return round(value / axis_max * CHART_PLOT_HEIGHT, 1) if axis_max else 0

    monthly_pnl = [
        {
            'name': calendar.month_abbr[month],
            'income': monthly_income[month - 1],
            'expenses': monthly_expenses[month - 1],
            'net': monthly_income[month - 1] - monthly_expenses[month - 1],
            'income_height': bar_height(monthly_income[month - 1]),
            'expense_height': bar_height(monthly_expenses[month - 1]),
        }
        for month in range(1, 13)
    ]

    axis_ticks = [
        {
            'value': axis_max * fraction / 4,
            'bottom': round(CHART_PLOT_HEIGHT * fraction / 4, 1),
        }
        for fraction in range(4, -1, -1)
    ] if axis_max else [{'value': 0, 'bottom': 0}]

    return monthly_pnl, axis_ticks


@app.route('/')
def index():
    now = datetime.datetime.utcnow()
    view_year = request.args.get('year', type=int) or now.year
    year_start = datetime.datetime(view_year, 1, 1)
    next_year_start = datetime.datetime(view_year + 1, 1, 1)

    incomes = Income.query.filter(
        Income.date >= year_start, Income.date < next_year_start
    ).order_by(Income.date.desc()).all()
    expenses = Expense.query.filter(
        Expense.date >= year_start, Expense.date < next_year_start
    ).order_by(Expense.date.desc()).all()

    monthly_pnl, axis_ticks = _build_yearly_chart(view_year)

    income_sources = [s.name for s in IncomeSource.query.order_by(IncomeSource.name).all()]
    expense_categories = [c.name for c in ExpenseCategory.query.order_by(ExpenseCategory.name).all()]

    income_usage = dict(
        expense_tracker.session.query(Income.source, func.count(Income.id)).group_by(Income.source).all()
    )
    expense_usage = dict(
        expense_tracker.session.query(Expense.category, func.count(Expense.id)).group_by(Expense.category).all()
    )
    income_sources_detailed = [
        {'id': s.id, 'name': s.name, 'count': income_usage.get(s.name, 0)}
        for s in IncomeSource.query.order_by(IncomeSource.name).all()
    ]
    expense_categories_detailed = [
        {'id': c.id, 'name': c.name, 'count': expense_usage.get(c.name, 0)}
        for c in ExpenseCategory.query.order_by(ExpenseCategory.name).all()
    ]

    return render_template(
        'index.html',
        active_page='dashboard',
        incomes=incomes,
        expenses=expenses,
        current_year=view_year,
        can_go_next_year=view_year < now.year,
        monthly_pnl=monthly_pnl,
        axis_ticks=axis_ticks,
        chart_plot_height=CHART_PLOT_HEIGHT,
        today=now.strftime('%Y-%m-%d'),
        income_sources=income_sources,
        expense_categories=expense_categories,
        income_sources_detailed=income_sources_detailed,
        expense_categories_detailed=expense_categories_detailed,
        income_entry_types=INCOME_ENTRY_TYPES,
        income_entry_type_labels=INCOME_ENTRY_TYPE_LABELS,
        income_shifts=INCOME_SHIFTS,
    )


def _parse_form_date(date_str):
    """Parse a yyyy-mm-dd form date, falling back to today (midnight) when blank.

    Always normalized to midnight so entries compare consistently regardless
    of what time of day they were actually submitted - otherwise two entries
    dated "today" could tiebreak on time-of-day instead of insertion order.
    """
    if not date_str:
        now = datetime.datetime.utcnow()
        return datetime.datetime(now.year, now.month, now.day), None
    try:
        return datetime.datetime.strptime(date_str, '%Y-%m-%d'), None
    except ValueError:
        return None, 'Date must be a valid date.'


@app.route('/add_expense', methods=['POST'])
def add_expense():
    expense_id = request.form.get('id')
    amount = request.form.get('amount')
    category = request.form.get('category')
    description = request.form.get('description')

    if not amount or not category:
        flash('Amount and category are required for an expense.')
        return redirect(url_for('index'))

    try:
        amount = float(amount)
    except ValueError:
        flash('Amount must be a number.')
        return redirect(url_for('index'))

    entry_date, error = _parse_form_date(request.form.get('date'))
    if error:
        flash(error)
        return redirect(url_for('index'))

    if expense_id:
        expense = Expense.query.get_or_404(expense_id)
        expense.amount = amount
        expense.category = category
        expense.description = description
        expense.date = entry_date
    else:
        expense_tracker.session.add(Expense(amount=amount, category=category, description=description, date=entry_date))

    expense_tracker.session.commit()

    return redirect(url_for('index'))

@app.route('/add_income', methods=['POST'])
def add_income():
    income_id = request.form.get('id')
    amount = request.form.get('amount')
    entry_type = request.form.get('entry_type') or 'tips'
    scheduled_hours = request.form.get('scheduled_hours')
    extra_hours = request.form.get('extra_hours') or '0'
    source = request.form.get('source')
    shift = request.form.get('shift') or None
    description = request.form.get('description')

    if entry_type not in INCOME_ENTRY_TYPES:
        flash('Invalid income type.')
        return redirect(url_for('index'))
    if shift and shift not in INCOME_SHIFTS:
        flash('Invalid shift.')
        return redirect(url_for('index'))

    # Only 'tips' entries represent hours actually worked - shift_pay/salary entries
    # are payments on hours already logged elsewhere, so they don't track hours.
    tracks_hours = entry_type == 'tips'

    if not amount or not source or (tracks_hours and not scheduled_hours):
        flash('Amount and source are required, and scheduled hours are required for tips entries.')
        return redirect(url_for('index'))

    try:
        amount = float(amount)
        scheduled_hours = float(scheduled_hours) if tracks_hours else 0.0
        extra_hours = float(extra_hours) if tracks_hours else 0.0
    except ValueError:
        flash('Amount, scheduled hours, and extra hours must be numbers.')
        return redirect(url_for('index'))

    entry_date, error = _parse_form_date(request.form.get('date'))
    if error:
        flash(error)
        return redirect(url_for('index'))

    if income_id:
        income = Income.query.get_or_404(income_id)
        income.amount = amount
        income.scheduled_hours = scheduled_hours
        income.extra_hours = extra_hours
        income.source = source
        income.entry_type = entry_type
        income.shift = shift
        income.description = description
        income.date = entry_date
    else:
        expense_tracker.session.add(Income(
            amount=amount, scheduled_hours=scheduled_hours, extra_hours=extra_hours,
            source=source, entry_type=entry_type, shift=shift, description=description, date=entry_date,
        ))

    expense_tracker.session.commit()

    return redirect(url_for('index'))


@app.route('/delete_income/<int:income_id>', methods=['POST'])
def delete_income(income_id):
    income = Income.query.get_or_404(income_id)
    expense_tracker.session.delete(income)
    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/delete_expense/<int:expense_id>', methods=['POST'])
def delete_expense(expense_id):
    expense = Expense.query.get_or_404(expense_id)
    expense_tracker.session.delete(expense)
    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/add_income_source', methods=['POST'])
def add_income_source():
    name = (request.form.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required.'}), 400

    existing = IncomeSource.query.filter_by(name=name).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name}), 200

    source = IncomeSource(name=name)
    expense_tracker.session.add(source)
    expense_tracker.session.commit()
    return jsonify({'id': source.id, 'name': source.name}), 201


@app.route('/add_expense_category', methods=['POST'])
def add_expense_category():
    name = (request.form.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required.'}), 400

    existing = ExpenseCategory.query.filter_by(name=name).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name}), 200

    category = ExpenseCategory(name=name)
    expense_tracker.session.add(category)
    expense_tracker.session.commit()
    return jsonify({'id': category.id, 'name': category.name}), 201


@app.route('/rename_income_source/<int:source_id>', methods=['POST'])
def rename_income_source(source_id):
    new_name = (request.form.get('name') or '').strip()
    source = IncomeSource.query.get_or_404(source_id)

    if not new_name:
        flash('Name cannot be empty.')
        return redirect(url_for('index'))

    old_name = source.name
    if new_name == old_name:
        return redirect(url_for('index'))

    existing = IncomeSource.query.filter_by(name=new_name).first()
    if existing and existing.id != source.id:
        # Merge: reassign every entry using the old name, then drop the now-redundant row.
        Income.query.filter_by(source=old_name).update({'source': new_name})
        expense_tracker.session.delete(source)
        flash(f'Merged "{old_name}" into existing source "{new_name}".')
    else:
        source.name = new_name
        Income.query.filter_by(source=old_name).update({'source': new_name})
        flash(f'Renamed "{old_name}" to "{new_name}".')

    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/delete_income_source/<int:source_id>', methods=['POST'])
def delete_income_source(source_id):
    source = IncomeSource.query.get_or_404(source_id)
    in_use = Income.query.filter_by(source=source.name).first() is not None
    if in_use:
        flash(f'"{source.name}" is used by existing income entries - rename it instead, or edit those entries first.')
        return redirect(url_for('index'))

    expense_tracker.session.delete(source)
    expense_tracker.session.commit()
    flash(f'Deleted source "{source.name}".')
    return redirect(url_for('index'))


@app.route('/rename_expense_category/<int:category_id>', methods=['POST'])
def rename_expense_category(category_id):
    new_name = (request.form.get('name') or '').strip()
    category = ExpenseCategory.query.get_or_404(category_id)

    if not new_name:
        flash('Name cannot be empty.')
        return redirect(url_for('index'))

    old_name = category.name
    if new_name == old_name:
        return redirect(url_for('index'))

    existing = ExpenseCategory.query.filter_by(name=new_name).first()
    if existing and existing.id != category.id:
        Expense.query.filter_by(category=old_name).update({'category': new_name})
        expense_tracker.session.delete(category)
        flash(f'Merged "{old_name}" into existing category "{new_name}".')
    else:
        category.name = new_name
        Expense.query.filter_by(category=old_name).update({'category': new_name})
        flash(f'Renamed "{old_name}" to "{new_name}".')

    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/delete_expense_category/<int:category_id>', methods=['POST'])
def delete_expense_category(category_id):
    category = ExpenseCategory.query.get_or_404(category_id)
    in_use = Expense.query.filter_by(category=category.name).first() is not None
    if in_use:
        flash(f'"{category.name}" is used by existing expense entries - rename it instead, or edit those entries first.')
        return redirect(url_for('index'))

    expense_tracker.session.delete(category)
    expense_tracker.session.commit()
    flash(f'Deleted category "{category.name}".')
    return redirect(url_for('index'))


def _shift_month(year, month, delta):
    total = (year * 12 + (month - 1)) + delta
    return total // 12, total % 12 + 1


def _month_bounds(year, month):
    start = datetime.datetime(year, month, 1)
    if month == 12:
        end = datetime.datetime(year + 1, 1, 1)
    else:
        end = datetime.datetime(year, month + 1, 1)
    return start, end


def _breakdown_rows(totals):
    total = sum(totals.values())
    max_value = max(totals.values(), default=0)
    rows = [
        {
            'label': name,
            'amount': amount,
            'pct_of_total': (amount / total * 100) if total else 0,
            'bar_pct': (amount / max_value * 100) if max_value else 0,
        }
        for name, amount in totals.items()
    ]
    rows.sort(key=lambda r: r['amount'], reverse=True)
    return rows, total


@app.route('/breakdown')
def breakdown():
    now = datetime.datetime.utcnow()
    year = request.args.get('year', type=int) or now.year
    month = request.args.get('month', type=int) or now.month
    month = min(max(month, 1), 12)

    month_start, next_month_start = _month_bounds(year, month)

    month_expenses = Expense.query.filter(Expense.date >= month_start, Expense.date < next_month_start).all()
    month_incomes = Income.query.filter(Income.date >= month_start, Income.date < next_month_start).all()

    expense_totals = {}
    for e in month_expenses:
        expense_totals[e.category] = expense_totals.get(e.category, 0.0) + e.amount
    income_totals = {}
    for i in month_incomes:
        income_totals[i.source] = income_totals.get(i.source, 0.0) + i.amount

    expense_rows, total_expenses = _breakdown_rows(expense_totals)
    income_rows, total_income = _breakdown_rows(income_totals)

    prev_year, prev_month = _shift_month(year, month, -1)
    next_year, next_month = _shift_month(year, month, 1)
    can_go_next = (year, month) < (now.year, now.month)

    return render_template(
        'breakdown.html',
        active_page='breakdown',
        month_label=datetime.date(year, month, 1).strftime('%B %Y'),
        year=year,
        month=month,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        can_go_next=can_go_next,
        expense_rows=expense_rows,
        income_rows=income_rows,
        total_expenses=total_expenses,
        total_income=total_income,
    )
