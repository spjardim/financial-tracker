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


@app.template_filter('money')
def money(value, decimals=2):
    """$1,234.50 / -$1,234.50 (Python's own format would put the sign after the $)."""
    sign = '-' if value < 0 else ''
    return f'{sign}${abs(value):,.{decimals}f}'


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
    sub_category = expense_tracker.Column(expense_tracker.String(50))
    place = expense_tracker.Column(expense_tracker.String(100))
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

class ExpenseSubCategory(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    category_id = expense_tracker.Column(
        expense_tracker.Integer, expense_tracker.ForeignKey('expense_category.id'), nullable=False
    )
    name = expense_tracker.Column(expense_tracker.String(100), nullable=False)

    __table_args__ = (
        expense_tracker.UniqueConstraint('category_id', 'name', name='uq_subcategory_per_category'),
    )

    def __repr__(self):
        return f'<ExpenseSubCategory {self.name}>'


class ExpensePlace(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    name = expense_tracker.Column(expense_tracker.String(100), unique=True, nullable=False)

    def __repr__(self):
        return f'<ExpensePlace {self.name}>'


ACCOUNT_TYPES = ['asset', 'investment', 'liability']
ACCOUNT_TYPE_LABELS = {'asset': 'Asset', 'investment': 'Investment', 'liability': 'Liability'}


class Account(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    name = expense_tracker.Column(expense_tracker.String(100), unique=True, nullable=False)
    account_type = expense_tracker.Column(expense_tracker.String(10), nullable=False)

    def __repr__(self):
        return f'<Account {self.name} - {self.account_type}>'


class AccountBalance(expense_tracker.Model):
    id = expense_tracker.Column(expense_tracker.Integer, primary_key=True)
    account_id = expense_tracker.Column(expense_tracker.Integer, expense_tracker.ForeignKey('account.id'), nullable=False)
    # Always normalized to the first of the month - net worth is a month-end snapshot, not a
    # dated transaction, so there is at most one balance per account per month.
    date = expense_tracker.Column(expense_tracker.DateTime, nullable=False)
    balance = expense_tracker.Column(expense_tracker.Float, nullable=False)

    __table_args__ = (
        expense_tracker.UniqueConstraint('account_id', 'date', name='uq_balance_per_account_month'),
    )

    def __repr__(self):
        return f'<AccountBalance {self.account_id} - {self.date} - {self.balance}>'


def _migrate_income_columns():
    """Backfill entry_type/shift onto an income table created before they existed."""
    with expense_tracker.engine.connect() as conn:
        existing_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(income)'))}
        if 'entry_type' not in existing_columns:
            conn.execute(text("ALTER TABLE income ADD COLUMN entry_type VARCHAR(20) NOT NULL DEFAULT 'tips'"))
        if 'shift' not in existing_columns:
            conn.execute(text('ALTER TABLE income ADD COLUMN shift VARCHAR(10)'))
        conn.commit()


def _migrate_expense_columns():
    """Backfill sub_category/place onto an expense table created before they existed."""
    with expense_tracker.engine.connect() as conn:
        existing_columns = {row[1] for row in conn.execute(text('PRAGMA table_info(expense)'))}
        if 'sub_category' not in existing_columns:
            conn.execute(text('ALTER TABLE expense ADD COLUMN sub_category VARCHAR(50)'))
        if 'place' not in existing_columns:
            conn.execute(text('ALTER TABLE expense ADD COLUMN place VARCHAR(100)'))
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
    _migrate_expense_columns()
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


def _chart_from_series(labels, income_values, expense_values):
    """Shared bar-height/axis-tick math for both the monthly (per-month-of-a-year)
    and yearly (per-calendar-year) P&L charts - only the bucketing resolution differs."""
    axis_max = _nice_ceil(max(income_values + expense_values, default=0))

    def bar_height(value):
        return round(value / axis_max * CHART_PLOT_HEIGHT, 1) if axis_max else 0

    periods = [
        {
            'name': label,
            'income': income_values[i],
            'expenses': expense_values[i],
            'net': income_values[i] - expense_values[i],
            'income_height': bar_height(income_values[i]),
            'expense_height': bar_height(expense_values[i]),
        }
        for i, label in enumerate(labels)
    ]

    axis_ticks = [
        {
            'value': axis_max * fraction / 4,
            'bottom': round(CHART_PLOT_HEIGHT * fraction / 4, 1),
        }
        for fraction in range(4, -1, -1)
    ] if axis_max else [{'value': 0, 'bottom': 0}]

    return periods, axis_ticks


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

    labels = [calendar.month_abbr[month] for month in range(1, 13)]
    return _chart_from_series(labels, monthly_income, monthly_expenses)


def _build_all_years_chart():
    """One column per calendar year that has any data at all - the all-time view."""
    all_incomes = Income.query.all()
    all_expenses = Expense.query.all()

    years = sorted({i.date.year for i in all_incomes} | {e.date.year for e in all_expenses})
    if not years:
        years = [datetime.datetime.utcnow().year]

    year_index = {year: i for i, year in enumerate(years)}
    yearly_income = [0.0] * len(years)
    yearly_expenses = [0.0] * len(years)
    for income in all_incomes:
        yearly_income[year_index[income.date.year]] += income.amount
    for expense in all_expenses:
        yearly_expenses[year_index[expense.date.year]] += expense.amount

    labels = [str(year) for year in years]
    return _chart_from_series(labels, yearly_income, yearly_expenses)


def _nice_floor(value):
    """Round down to a clean negative axis min - the mirror of _nice_ceil for the
    below-zero side, since net worth (unlike income/expenses) can go negative."""
    return -_nice_ceil(-value) if value < 0 else 0


def _build_net_worth_chart():
    """One point per month that has any recorded balance. The 'Update Balances' form
    always re-submits every account (pre-filled with its last value), so a month's
    total assets/liabilities is just the sum of that month's rows - no carry-forward
    needed for months where the user left an unchanged account's figure as-is."""
    accounts_by_id = {a.id: a for a in Account.query.all()}
    balances = AccountBalance.query.order_by(AccountBalance.date).all()
    if not balances:
        return None

    months = sorted({b.date for b in balances})
    assets_by_month = []
    investments_by_month = []
    liabilities_by_month = []
    net_by_month = []
    for month in months:
        month_balances = [b for b in balances if b.date == month]

        def month_total(account_type):
            return sum(b.balance for b in month_balances if accounts_by_id[b.account_id].account_type == account_type)

        assets, investments, liabilities = month_total('asset'), month_total('investment'), month_total('liability')
        assets_by_month.append(assets)
        investments_by_month.append(investments)
        liabilities_by_month.append(liabilities)
        net_by_month.append(assets + investments - liabilities)

    axis_max = _nice_ceil(max(net_by_month + [0]))
    axis_min = _nice_floor(min(net_by_month + [0]))
    span = (axis_max - axis_min) or 1

    def y_for(value):
        return round((axis_max - value) / span * CHART_PLOT_HEIGHT, 1)

    point_count = len(months)
    points = [
        {
            'x': round((i / (point_count - 1) * 1000) if point_count > 1 else 500, 1),
            'y': y_for(value),
            'label': month.strftime('%b %Y'),
            'value': value,
        }
        for i, (month, value) in enumerate(zip(months, net_by_month))
    ]

    tick_count = 4
    axis_ticks = [
        {
            'value': axis_min + (axis_max - axis_min) * step / tick_count,
            'bottom': round((axis_min + (axis_max - axis_min) * step / tick_count - axis_min) / span * CHART_PLOT_HEIGHT, 1),
        }
        for step in range(tick_count, -1, -1)
    ]
    zero_bottom = round((0 - axis_min) / span * CHART_PLOT_HEIGHT, 1) if axis_min < 0 else None
    if zero_bottom is not None:
        axis_ticks = [t for t in axis_ticks if abs(t['bottom'] - zero_bottom) > 20]

    return {
        'points': points,
        'axis_ticks': axis_ticks,
        # Evenly spaced ticks don't land on $0 when the range spans negatives, so the
        # breakeven baseline is drawn separately at its true position.
        'zero_bottom': zero_bottom,
        'first_label': months[0].strftime('%b %Y'),
        'last_label': months[-1].strftime('%b %Y'),
        'current_assets': assets_by_month[-1],
        'current_investments': investments_by_month[-1],
        'has_investments': any(a.account_type == 'investment' for a in accounts_by_id.values()),
        'current_liabilities': liabilities_by_month[-1],
        'current_net_worth': net_by_month[-1],
        'prev_net_worth': net_by_month[-2] if point_count > 1 else None,
    }


def _category_totals(expenses):
    totals = {}
    for e in expenses:
        totals[e.category] = totals.get(e.category, 0.0) + e.amount
    return totals


def _source_totals(incomes):
    totals = {}
    for i in incomes:
        totals[i.source] = totals.get(i.source, 0.0) + i.amount
    return totals


def _place_totals(expenses):
    totals = {}
    for e in expenses:
        label = e.place or 'No Place'
        totals[label] = totals.get(label, 0.0) + e.amount
    return totals


def _avg_hourly_rate(incomes):
    """Weighted average $/hr across 'tips' entries - the only entries that track hours."""
    tip_incomes = [i for i in incomes if i.entry_type == 'tips' and (i.scheduled_hours + i.extra_hours) > 0]
    total_hours = sum(i.scheduled_hours + i.extra_hours for i in tip_incomes)
    if not total_hours:
        return None
    return sum(i.amount for i in tip_incomes) / total_hours


def _pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / previous * 100


@app.route('/')
def index():
    now = datetime.datetime.utcnow()
    view = request.args.get('view')
    view = view if view in ('monthly', 'yearly') else 'monthly'
    view_year = request.args.get('year', type=int) or now.year

    if view == 'yearly':
        # All-time: one column per calendar year, so there's no single period to page through.
        period_start, period_end = None, None
        chart_periods, axis_ticks = _build_all_years_chart()
        period_label = 'All-Time'
        prev_year, next_year, can_go_next = None, None, False
    else:
        period_start = datetime.datetime(view_year, 1, 1)
        period_end = datetime.datetime(view_year + 1, 1, 1)
        chart_periods, axis_ticks = _build_yearly_chart(view_year)
        period_label = str(view_year)
        prev_year, next_year = view_year - 1, view_year + 1
        can_go_next = view_year < now.year

    if period_start is not None:
        incomes = Income.query.filter(
            Income.date >= period_start, Income.date < period_end
        ).order_by(Income.date.desc()).all()
        expenses = Expense.query.filter(
            Expense.date >= period_start, Expense.date < period_end
        ).order_by(Expense.date.desc()).all()
    else:
        incomes = Income.query.order_by(Income.date.desc()).all()
        expenses = Expense.query.order_by(Expense.date.desc()).all()

    expense_rows, total_expenses = _breakdown_rows(_category_totals(expenses))
    income_rows, total_income = _breakdown_rows(_source_totals(incomes))
    place_rows, _ = _breakdown_rows(_place_totals(expenses))

    net_savings = total_income - total_expenses
    savings_rate = (net_savings / total_income * 100) if total_income else None
    avg_hourly_rate = _avg_hourly_rate(incomes)

    if view == 'monthly':
        prev_year_start = datetime.datetime(view_year - 1, 1, 1)
        prev_income_total = expense_tracker.session.query(func.coalesce(func.sum(Income.amount), 0.0)).filter(
            Income.date >= prev_year_start, Income.date < period_start
        ).scalar()
        prev_expense_total = expense_tracker.session.query(func.coalesce(func.sum(Expense.amount), 0.0)).filter(
            Expense.date >= prev_year_start, Expense.date < period_start
        ).scalar()
        income_delta_pct = _pct_change(total_income, prev_income_total)
        expense_delta_pct = _pct_change(total_expenses, prev_expense_total)
    else:
        income_delta_pct = None
        expense_delta_pct = None

    # Net worth is a running balance, not a flow, so it ignores the monthly/yearly toggle.
    net_worth = _build_net_worth_chart()
    net_worth_delta_pct = None
    if net_worth and net_worth['prev_net_worth']:
        net_worth_delta_pct = (
            (net_worth['current_net_worth'] - net_worth['prev_net_worth']) / abs(net_worth['prev_net_worth']) * 100
        )
    latest_balance_by_account = {}
    for b in AccountBalance.query.order_by(AccountBalance.date).all():
        latest_balance_by_account[b.account_id] = b.balance
    accounts_detailed = [
        {
            'id': a.id,
            'name': a.name,
            'account_type': a.account_type,
            'latest_balance': latest_balance_by_account.get(a.id),
            'has_balances': a.id in latest_balance_by_account,
        }
        for a in Account.query.order_by(Account.name).all()
    ]

    income_sources = [s.name for s in IncomeSource.query.order_by(IncomeSource.name).all()]
    expense_categories = [c.name for c in ExpenseCategory.query.order_by(ExpenseCategory.name).all()]
    expense_places = [p.name for p in ExpensePlace.query.order_by(ExpensePlace.name).all()]
    income_usage = dict(
        expense_tracker.session.query(Income.source, func.count(Income.id)).group_by(Income.source).all()
    )
    expense_usage = dict(
        expense_tracker.session.query(Expense.category, func.count(Expense.id)).group_by(Expense.category).all()
    )
    # Keyed by (category name, sub_category name) since Expense only stores names, not the
    # ExpenseSubCategory id, and sub_category names are only unique within their own category.
    expense_subcategory_usage = {
        (category, sub_category): count
        for category, sub_category, count in expense_tracker.session.query(
            Expense.category, Expense.sub_category, func.count(Expense.id)
        ).group_by(Expense.category, Expense.sub_category).all()
    }
    expense_place_usage = dict(
        expense_tracker.session.query(Expense.place, func.count(Expense.id)).group_by(Expense.place).all()
    )
    income_sources_detailed = [
        {'id': s.id, 'name': s.name, 'count': income_usage.get(s.name, 0)}
        for s in IncomeSource.query.order_by(IncomeSource.name).all()
    ]
    expense_categories_all = ExpenseCategory.query.order_by(ExpenseCategory.name).all()
    expense_categories_detailed = [
        {'id': c.id, 'name': c.name, 'count': expense_usage.get(c.name, 0)}
        for c in expense_categories_all
    ]
    category_name_by_id = {c.id: c.name for c in expense_categories_all}
    expense_category_ids = {c.name: c.id for c in expense_categories_all}
    expense_subcategories_detailed = [
        {
            'id': s.id,
            'name': s.name,
            'category_id': s.category_id,
            'count': expense_subcategory_usage.get((category_name_by_id.get(s.category_id), s.name), 0),
        }
        for s in ExpenseSubCategory.query.order_by(ExpenseSubCategory.name).all()
    ]
    expense_places_detailed = [
        {'id': p.id, 'name': p.name, 'count': expense_place_usage.get(p.name, 0)}
        for p in ExpensePlace.query.order_by(ExpensePlace.name).all()
    ]
    return render_template(
        'index.html',
        active_page='dashboard',
        view=view,
        view_year=view_year,
        period_label=period_label,
        prev_year=prev_year,
        next_year=next_year,
        can_go_next=can_go_next,
        incomes=incomes,
        expenses=expenses,
        chart_periods=chart_periods,
        axis_ticks=axis_ticks,
        chart_plot_height=CHART_PLOT_HEIGHT,
        today=now.strftime('%Y-%m-%d'),
        expense_rows=expense_rows,
        income_rows=income_rows,
        place_rows=place_rows,
        total_expenses=total_expenses,
        total_income=total_income,
        net_savings=net_savings,
        savings_rate=savings_rate,
        avg_hourly_rate=avg_hourly_rate,
        income_delta_pct=income_delta_pct,
        expense_delta_pct=expense_delta_pct,
        net_worth=net_worth,
        net_worth_delta_pct=net_worth_delta_pct,
        accounts_detailed=accounts_detailed,
        account_type_labels=ACCOUNT_TYPE_LABELS,
        current_month_value=now.strftime('%Y-%m'),
        donut_radius=DONUT_RADIUS,
        income_sources=income_sources,
        expense_categories=expense_categories,
        expense_places=expense_places,
        expense_category_ids=expense_category_ids,
        income_sources_detailed=income_sources_detailed,
        expense_categories_detailed=expense_categories_detailed,
        expense_subcategories_detailed=expense_subcategories_detailed,
        expense_places_detailed=expense_places_detailed,
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
    sub_category = (request.form.get('sub_category') or '').strip() or 'Misc'
    place = (request.form.get('place') or '').strip() or None
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

    # Place is free-typed with suggestions rather than picked from a locked list, so it grows
    # the lookup table on its own instead of requiring a separate "add" step like category does.
    if place and not ExpensePlace.query.filter_by(name=place).first():
        expense_tracker.session.add(ExpensePlace(name=place))

    if expense_id:
        expense = Expense.query.get_or_404(expense_id)
        expense.amount = amount
        expense.category = category
        expense.sub_category = sub_category
        expense.place = place
        expense.description = description
        expense.date = entry_date
    else:
        expense_tracker.session.add(Expense(amount=amount, category=category, sub_category=sub_category, place=place, description=description, date=entry_date))

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


@app.route('/add_expense_subcategory', methods=['POST'])
def add_expense_subcategory():
    name = (request.form.get('name') or '').strip()
    category_id = request.form.get('category_id', type=int)

    if not name or not category_id:
        return jsonify({'error': 'Name and category are required.'}), 400

    if not ExpenseCategory.query.get(category_id):
        return jsonify({'error': 'Category not found.'}), 400

    existing = ExpenseSubCategory.query.filter_by(category_id=category_id, name=name).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name}), 200

    subcategory = ExpenseSubCategory(category_id=category_id, name=name)
    expense_tracker.session.add(subcategory)
    expense_tracker.session.commit()
    return jsonify({'id': subcategory.id, 'name': subcategory.name}), 201

@app.route('/add_expense_place', methods=['POST'])
def add_expense_place():
    name = (request.form.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Name is required.'}), 400

    existing = ExpensePlace.query.filter_by(name=name).first()
    if existing:
        return jsonify({'id': existing.id, 'name': existing.name}), 200

    place = ExpensePlace(name=name)
    expense_tracker.session.add(place)
    expense_tracker.session.commit()
    return jsonify({'id': place.id, 'name': place.name}), 201

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


@app.route('/rename_expense_subcategory/<int:subcategory_id>', methods=['POST'])
def rename_expense_subcategory(subcategory_id):
    new_name = (request.form.get('name') or '').strip()
    subcategory = ExpenseSubCategory.query.get_or_404(subcategory_id)
    category = ExpenseCategory.query.get_or_404(subcategory.category_id)

    if not new_name:
        flash('Name cannot be empty.')
        return redirect(url_for('index'))

    old_name = subcategory.name
    if new_name == old_name:
        return redirect(url_for('index'))

    # Uniqueness is scoped to the category, so only a subcategory under the same category collides.
    existing = ExpenseSubCategory.query.filter_by(category_id=subcategory.category_id, name=new_name).first()
    if existing and existing.id != subcategory.id:
        Expense.query.filter_by(category=category.name, sub_category=old_name).update({'sub_category': new_name})
        expense_tracker.session.delete(subcategory)
        flash(f'Merged "{old_name}" into existing subcategory "{new_name}".')
    else:
        subcategory.name = new_name
        Expense.query.filter_by(category=category.name, sub_category=old_name).update({'sub_category': new_name})
        flash(f'Renamed "{old_name}" to "{new_name}".')

    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/delete_expense_subcategory/<int:subcategory_id>', methods=['POST'])
def delete_expense_subcategory(subcategory_id):
    subcategory = ExpenseSubCategory.query.get_or_404(subcategory_id)
    category = ExpenseCategory.query.get_or_404(subcategory.category_id)
    in_use = Expense.query.filter_by(category=category.name, sub_category=subcategory.name).first() is not None
    if in_use:
        flash(f'"{subcategory.name}" is used by existing expense entries - rename it instead, or edit those entries first.')
        return redirect(url_for('index'))

    expense_tracker.session.delete(subcategory)
    expense_tracker.session.commit()
    flash(f'Deleted subcategory "{subcategory.name}".')
    return redirect(url_for('index'))


@app.route('/rename_expense_place/<int:place_id>', methods=['POST'])
def rename_expense_place(place_id):
    new_name = (request.form.get('name') or '').strip()
    place = ExpensePlace.query.get_or_404(place_id)

    if not new_name:
        flash('Name cannot be empty.')
        return redirect(url_for('index'))

    old_name = place.name
    if new_name == old_name:
        return redirect(url_for('index'))

    existing = ExpensePlace.query.filter_by(name=new_name).first()
    if existing and existing.id != place.id:
        Expense.query.filter_by(place=old_name).update({'place': new_name})
        expense_tracker.session.delete(place)
        flash(f'Merged "{old_name}" into existing place "{new_name}".')
    else:
        place.name = new_name
        Expense.query.filter_by(place=old_name).update({'place': new_name})
        flash(f'Renamed "{old_name}" to "{new_name}".')

    expense_tracker.session.commit()
    return redirect(url_for('index'))


@app.route('/delete_expense_place/<int:place_id>', methods=['POST'])
def delete_expense_place(place_id):
    place = ExpensePlace.query.get_or_404(place_id)
    in_use = Expense.query.filter_by(place=place.name).first() is not None
    if in_use:
        flash(f'"{place.name}" is used by existing expense entries - rename it instead, or edit those entries first.')
        return redirect(url_for('index'))

    expense_tracker.session.delete(place)
    expense_tracker.session.commit()
    flash(f'Deleted place "{place.name}".')
    return redirect(url_for('index'))


@app.route('/add_account', methods=['POST'])
def add_account():
    name = (request.form.get('name') or '').strip()
    account_type = request.form.get('account_type')
    if not name or account_type not in ACCOUNT_TYPES:
        flash('Account name and a valid type are required.')
        return redirect(url_for('index'))
    if Account.query.filter_by(name=name).first():
        flash(f'"{name}" already exists.')
        return redirect(url_for('index'))

    expense_tracker.session.add(Account(name=name, account_type=account_type))
    expense_tracker.session.commit()
    flash(f'Added account "{name}".')
    return redirect(url_for('index'))


@app.route('/rename_account/<int:account_id>', methods=['POST'])
def rename_account(account_id):
    new_name = (request.form.get('name') or '').strip()
    account = Account.query.get_or_404(account_id)
    if not new_name:
        flash('Name cannot be empty.')
        return redirect(url_for('index'))
    if new_name == account.name:
        return redirect(url_for('index'))
    if Account.query.filter_by(name=new_name).first():
        flash(f'"{new_name}" already exists.')
        return redirect(url_for('index'))

    account.name = new_name
    expense_tracker.session.commit()
    flash(f'Renamed account to "{new_name}".')
    return redirect(url_for('index'))


@app.route('/delete_account/<int:account_id>', methods=['POST'])
def delete_account(account_id):
    account = Account.query.get_or_404(account_id)
    if AccountBalance.query.filter_by(account_id=account.id).first() is not None:
        flash(f'"{account.name}" has recorded balances, so it can\'t be deleted.')
        return redirect(url_for('index'))

    expense_tracker.session.delete(account)
    expense_tracker.session.commit()
    flash(f'Deleted account "{account.name}".')
    return redirect(url_for('index'))


@app.route('/save_balances', methods=['POST'])
def save_balances():
    try:
        year, month = (int(part) for part in (request.form.get('month') or '').split('-'))
        month_date = datetime.datetime(year, month, 1)
    except ValueError:
        flash('Pick a valid month.')
        return redirect(url_for('index'))

    saved = 0
    for account in Account.query.all():
        raw = (request.form.get(f'balance_{account.id}') or '').strip()
        if not raw:
            continue
        try:
            balance = float(raw)
        except ValueError:
            flash(f'Balance for "{account.name}" must be a number.')
            return redirect(url_for('index'))

        existing = AccountBalance.query.filter_by(account_id=account.id, date=month_date).first()
        if existing:
            existing.balance = balance
        else:
            expense_tracker.session.add(AccountBalance(account_id=account.id, date=month_date, balance=balance))
        saved += 1

    expense_tracker.session.commit()
    flash(f'Saved {saved} balance(s) for {month_date.strftime("%B %Y")}.')
    return redirect(url_for('index'))


# Validated categorical hues (dark-mode steps, checked against this app's card
# surface #10141d) for the breakdown donut charts - fixed order, never cycled.
DONUT_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9']
DONUT_OTHER_COLOR = '#7b8492'  # matches --text-muted; used for the folded "Other" slice
DONUT_MAX_SLICES = len(DONUT_COLORS)
DONUT_RADIUS = 70
DONUT_CIRCUMFERENCE = 2 * math.pi * DONUT_RADIUS
DONUT_GAP = 3  # stroke-dasharray units of breathing room between adjacent slices


def _breakdown_rows(totals):
    total = sum(totals.values())
    max_value = max(totals.values(), default=0)

    # Past the token ceiling, fold the smallest slices into a single "Other" slice
    # instead of generating more hues - a categorical palette only has so many
    # CVD-distinct slots, and beyond them a new hue is indistinguishable from an
    # existing one anyway.
    sorted_items = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    if len(sorted_items) > DONUT_MAX_SLICES:
        head, tail = sorted_items[:DONUT_MAX_SLICES], sorted_items[DONUT_MAX_SLICES:]
        sorted_items = head + [('Other', sum(amount for _, amount in tail))]

    rows = []
    cumulative_length = 0.0
    for index, (name, amount) in enumerate(sorted_items):
        pct = (amount / total * 100) if total else 0
        segment_length = pct / 100 * DONUT_CIRCUMFERENCE
        # A lone slice fills the whole ring, so it shouldn't have a gap carved out of it.
        visible_length = max(segment_length - DONUT_GAP, 0) if len(sorted_items) > 1 else segment_length
        rows.append({
            'label': name,
            'amount': amount,
            'pct_of_total': pct,
            'bar_pct': (amount / max_value * 100) if max_value else 0,
            'color': DONUT_COLORS[index] if index < len(DONUT_COLORS) else DONUT_OTHER_COLOR,
            'dash_array': f'{visible_length:.2f} {max(DONUT_CIRCUMFERENCE - visible_length, 0):.2f}',
            'dash_offset': round(-cumulative_length, 2),
        })
        cumulative_length += segment_length

    return rows, total


@app.route('/breakdown')
def breakdown():
    # The breakdown page merged into the single dashboard view - keep old links/bookmarks working.
    # There's no single-month view anymore, so land on the whole year the old link pointed at.
    args = {'view': 'monthly'}
    year = request.args.get('year', type=int)
    if year:
        args['year'] = year
    return redirect(url_for('index', **args))
