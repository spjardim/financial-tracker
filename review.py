"""The review inbox. Every posted bank transaction waits here until the user files it as an expense,
income, money paid back toward an earlier expense, or ignores it. Suggestions come from the user's
last decision for the same merchant first, then from how they've filed that Plaid category before,
then from Plaid's own category."""
import datetime
import re

from flask import flash, redirect, render_template, request, url_for

from app import (
    app, expense_tracker as db, Account, BankTransaction, Expense, ExpenseCategory, ExpensePlace,
    ExpenseSubCategory, Income, IncomeSource, PlaidAccount, PlaidCategoryMap, Reimbursement, ReviewRule,
    INCOME_ENTRY_TYPES, INCOME_ENTRY_TYPE_LABELS, INCOME_SHIFTS,
)

# Card payments, transfers between your own accounts, loan payments: money moving, not spent or earned.
MONEY_MOVEMENT_CATEGORIES = {'TRANSFER_IN', 'TRANSFER_OUT', 'LOAN_PAYMENTS'}
TRUSTED_CONFIDENCE = {'VERY_HIGH', 'HIGH'}
PAYBACK_LOOKBACK_DAYS = 365
RECENTLY_REVIEWED_LIMIT = 30
CENT = 0.005


class ReviewError(Exception):
    pass


@app.context_processor
def inject_review_count():
    return {'review_count': BankTransaction.query.filter_by(status='new').count()}


@app.route('/review')
def review():
    context = _suggestion_context()
    account_names = _account_names()
    rows = []
    for line in BankTransaction.query.filter_by(status='new').order_by(BankTransaction.date.desc(), BankTransaction.id.desc()):
        suggestion = _suggest(line, context)
        rows.append({
            'line': line,
            'suggestion': suggestion,
            'account': account_names.get(line.plaid_account_id, 'Untracked account'),
            'ready': _ready(suggestion),
        })

    recent = (BankTransaction.query.filter(BankTransaction.status != 'new', BankTransaction.reviewed_at.isnot(None))
              .order_by(BankTransaction.reviewed_at.desc()).limit(RECENTLY_REVIEWED_LIMIT).all())

    categories = ExpenseCategory.query.order_by(ExpenseCategory.name).all()
    subcategories_by_category = {c.name: [] for c in categories}
    category_names_by_id = {c.id: c.name for c in categories}
    for s in ExpenseSubCategory.query.order_by(ExpenseSubCategory.name):
        if s.category_id in category_names_by_id:
            subcategories_by_category[category_names_by_id[s.category_id]].append(s.name)

    return render_template(
        'review.html',
        active_page='review',
        rows=rows,
        ready_count=sum(1 for r in rows if r['ready']),
        recent=[{'line': line, 'outcome': _describe_outcome(line), 'account': account_names.get(line.plaid_account_id, '')}
                for line in recent],
        category_names=[c.name for c in categories],
        subcategories_by_category=subcategories_by_category,
        place_names=[p.name for p in ExpensePlace.query.order_by(ExpensePlace.name)],
        source_names=[s.name for s in IncomeSource.query.order_by(IncomeSource.name)],
        expense_choices=[{'id': e.id, 'label': _expense_label(e)} for e in context['payback_candidates']],
        income_entry_types=INCOME_ENTRY_TYPES,
        income_entry_type_labels=INCOME_ENTRY_TYPE_LABELS,
        income_shifts=INCOME_SHIFTS,
    )


@app.route('/review/<int:line_id>', methods=['POST'])
def review_file(line_id):
    line = BankTransaction.query.get_or_404(line_id)
    if line.status != 'new':
        flash('That transaction was already filed.')
        return redirect(url_for('review'))
    form = request.form
    try:
        action = form.get('action')
        if action == 'expense':
            _file_expense(line, form.get('category'), form.get('sub_category'), form.get('place'))
        elif action == 'income':
            _file_income(line, form.get('income_source'), form.get('income_type'), form.get('amount'),
                         form.get('shift'), form.get('scheduled_hours'), form.get('extra_hours'))
        elif action == 'payback':
            _file_payback(line, form.get('expense_id', type=int))
        elif action == 'ignore':
            _file_ignore(line)
        else:
            raise ReviewError('Pick what this transaction is.')
    except ReviewError as exc:
        db.session.rollback()
        flash(f'{line.merchant_name or line.description}: {exc}')
        return redirect(url_for('review'))
    db.session.commit()
    return redirect(url_for('review'))


@app.route('/review/approve', methods=['POST'])
def review_approve_all():
    """File every line whose suggestion is complete and trusted; leave the rest for the user."""
    context = _suggestion_context()
    filed = 0
    for line in BankTransaction.query.filter_by(status='new').order_by(BankTransaction.date, BankTransaction.id):
        suggestion = _suggest(line, context)
        if not _ready(suggestion):
            continue
        try:
            if suggestion['action'] == 'expense':
                _file_expense(line, suggestion['category'], suggestion['sub_category'], suggestion['place'])
            elif suggestion['action'] == 'income':
                _file_income(line, suggestion['income_source'], suggestion['income_type'], None, None, None, None)
            else:
                _file_ignore(line)
        except ReviewError:
            continue  # validation runs before any write, so the line is simply left for the user
        filed += 1
    db.session.commit()
    left = BankTransaction.query.filter_by(status='new').count()
    flash(f'Filed {filed} transaction(s) as suggested.' + (f' {left} still need a look.' if left else ''))
    return redirect(url_for('review'))


@app.route('/review/<int:line_id>/undo', methods=['POST'])
def review_undo(line_id):
    """Put a reviewed line back in the inbox, removing whatever it was filed as."""
    line = BankTransaction.query.get_or_404(line_id)
    if line.status == 'new':
        return redirect(url_for('review'))
    if line.expense_id:
        expense = db.session.get(Expense, line.expense_id)
        if expense and Reimbursement.query.filter_by(expense_id=expense.id).first():
            flash('Money has been paid back toward that expense - undo those paybacks first.')
            return redirect(url_for('review'))
        if expense:
            db.session.delete(expense)
    if line.income_id:
        income = db.session.get(Income, line.income_id)
        if income:
            db.session.delete(income)
    if line.reimbursement_id:
        payback = db.session.get(Reimbursement, line.reimbursement_id)
        if payback:
            expense = db.session.get(Expense, payback.expense_id)
            if expense:
                expense.reimbursed = max((expense.reimbursed or 0.0) - payback.amount, 0.0)
            db.session.delete(payback)
    line.status = 'new'
    line.expense_id = line.income_id = line.reimbursement_id = None
    line.reviewed_at = None
    db.session.commit()
    flash(f'Moved "{line.merchant_name or line.description}" back to To Review.')
    return redirect(url_for('review'))


def _suggestion_context():
    since = datetime.datetime.utcnow() - datetime.timedelta(days=PAYBACK_LOOKBACK_DAYS)
    candidates = [
        e for e in Expense.query.filter(Expense.date >= since).order_by(Expense.date.desc(), Expense.id.desc())
        if e.net_amount > CENT
    ]
    return {
        'rules': {r.key: r for r in ReviewRule.query.all()},
        'category_map': {m.pfc_detailed: (m.category, m.sub_category) for m in PlaidCategoryMap.query.all()},
        'payback_candidates': candidates,
    }


def _suggest(line, context):
    """What this line most likely is. `basis` says why: 'rule' (the user's past choice), 'plaid'
    (Plaid was confident), or 'unsure' (Plaid's low-confidence guess - never auto-approved)."""
    money_out = line.amount > 0
    suggestion = {'action': 'ignore', 'category': None, 'sub_category': None, 'place': line.merchant_name,
                  'income_source': None, 'income_type': None, 'expense_id': None, 'basis': 'plaid'}

    rule = context['rules'].get(_rule_key(line))
    if rule:
        suggestion.update(action=rule.action, category=rule.category, sub_category=rule.sub_category,
                          place=rule.place or line.merchant_name, income_source=rule.income_source,
                          income_type=rule.income_type, basis='rule')
        return suggestion

    confident = line.pfc_confidence in TRUSTED_CONFIDENCE
    if line.pfc_primary in MONEY_MOVEMENT_CATEGORIES:
        suggestion['basis'] = 'plaid' if confident else 'unsure'
    elif money_out:
        category, sub_category = _category_for(line, context['category_map'])
        suggestion.update(action='expense', category=category, sub_category=sub_category,
                          basis='plaid' if confident else 'unsure')
    elif line.pfc_primary == 'INCOME':
        suggestion.update(action='income', income_type='shift_pay' if 'WAGES' in (line.pfc_detailed or '') else 'other')
    else:
        match = _matching_expense(line, context['payback_candidates'])
        if match:
            suggestion.update(action='payback', expense_id=match.id)
        else:
            suggestion['basis'] = 'unsure'
    return suggestion


def _ready(suggestion):
    """Whether Approve All can file this line without the user looking at it."""
    if suggestion['basis'] == 'unsure':
        return False
    if suggestion['action'] == 'expense':
        return bool(suggestion['category'])
    if suggestion['action'] == 'income':
        return bool(suggestion['income_source']) and suggestion['income_type'] in INCOME_ENTRY_TYPES \
            and suggestion['income_type'] != 'tips'  # tips need this shift's hours
    return suggestion['action'] == 'ignore'


def _rule_key(line):
    """Merchant identity for remembered decisions: digits and punctuation dropped (store numbers,
    dates, reference ids vary between charges), and money in/out kept apart (a store's refund
    shouldn't be suggested as a purchase)."""
    words = re.sub(r'[^A-Z]+', ' ', (line.merchant_name or line.description or '').upper()).split()
    if not words:
        return None
    return f"{'out' if line.amount > 0 else 'in'}:{' '.join(words)}"[:160]


def _category_for(line, category_map):
    if not line.pfc_detailed:
        return 'Uncategorized', 'Misc'
    if line.pfc_detailed in category_map:
        return category_map[line.pfc_detailed]
    primary = line.pfc_primary or ''
    prefix = primary + '_'
    detail = line.pfc_detailed[len(prefix):] if line.pfc_detailed.startswith(prefix) else line.pfc_detailed
    return (_humanize(primary) or 'Uncategorized')[:50], (_humanize(detail) or 'Misc')[:50]


def _matching_expense(line, candidates):
    """Most recent earlier expense at the same place with enough left to cover this refund."""
    merchant = (line.merchant_name or '').lower()
    if not merchant:
        return None
    refund = -line.amount
    for expense in candidates:
        if expense.date <= line.date and (expense.place or '').lower() == merchant and expense.net_amount + CENT >= refund:
            return expense
    return None


def _file_expense(line, category, sub_category, place):
    if line.amount <= 0:
        raise ReviewError("money coming in can't be an expense - file it as income or as a payback.")
    category = (category or '').strip()[:50]
    if not category:
        raise ReviewError('pick a category.')
    sub_category = (sub_category or '').strip()[:50] or 'Misc'
    place = (place or '').strip()[:100] or None

    category_row = ExpenseCategory.query.filter_by(name=category).first()
    if category_row is None:
        category_row = ExpenseCategory(name=category)
        db.session.add(category_row)
        db.session.flush()
    if sub_category != 'Misc' and not ExpenseSubCategory.query.filter_by(category_id=category_row.id, name=sub_category).first():
        db.session.add(ExpenseSubCategory(category_id=category_row.id, name=sub_category))
    if place and not ExpensePlace.query.filter_by(name=place).first():
        db.session.add(ExpensePlace(name=place))

    expense = Expense(amount=line.amount, category=category, sub_category=sub_category, place=place,
                      description=line.description, date=line.date)
    db.session.add(expense)
    db.session.flush()
    _mark_reviewed(line, 'expense', expense_id=expense.id)
    _remember(line, action='expense', category=category, sub_category=sub_category, place=place)
    if line.pfc_detailed:
        mapping = PlaidCategoryMap.query.filter_by(pfc_detailed=line.pfc_detailed).first()
        if mapping is None:
            mapping = PlaidCategoryMap(pfc_detailed=line.pfc_detailed)
            db.session.add(mapping)
        mapping.category, mapping.sub_category = category, sub_category


def _file_income(line, source, entry_type, amount, shift, scheduled_hours, extra_hours):
    if line.amount >= 0:
        raise ReviewError("money going out can't be income.")
    source = (source or '').strip()[:50]
    if not source:
        raise ReviewError('pick an income source.')
    if entry_type not in INCOME_ENTRY_TYPES:
        raise ReviewError('pick an income type.')
    if shift and shift not in INCOME_SHIFTS:
        raise ReviewError('pick a valid shift.')
    try:
        amount = float(amount) if amount not in (None, '') else -line.amount
        tracks_hours = entry_type == 'tips'
        scheduled_hours = float(scheduled_hours) if tracks_hours else 0.0
        extra_hours = float(extra_hours or 0) if tracks_hours else 0.0
    except (TypeError, ValueError):
        raise ReviewError('amount and hours must be numbers (tips need scheduled hours).') from None
    if amount <= 0:
        raise ReviewError('amount must be more than zero.')

    if not IncomeSource.query.filter_by(name=source).first():
        db.session.add(IncomeSource(name=source))
    income = Income(amount=amount, scheduled_hours=scheduled_hours, extra_hours=extra_hours, source=source,
                    entry_type=entry_type, shift=shift or None, description=line.description, date=line.date)
    db.session.add(income)
    db.session.flush()
    _mark_reviewed(line, 'income', income_id=income.id)
    _remember(line, action='income', income_source=source, income_type=entry_type)


def _file_payback(line, expense_id):
    if line.amount >= 0:
        raise ReviewError('only money coming in can pay back an expense.')
    expense = db.session.get(Expense, expense_id) if expense_id else None
    if expense is None:
        raise ReviewError('pick the expense this pays back.')
    amount = -line.amount
    if amount > expense.net_amount + CENT:
        raise ReviewError(f'${amount:,.2f} is more than the ${expense.net_amount:,.2f} still unpaid on that expense - '
                          'pick another expense or file it as income.')
    payback = Reimbursement(expense_id=expense.id, amount=amount, date=line.date, description=line.description)
    db.session.add(payback)
    db.session.flush()
    expense.reimbursed = (expense.reimbursed or 0.0) + amount
    _mark_reviewed(line, 'payback', reimbursement_id=payback.id)


def _file_ignore(line):
    _mark_reviewed(line, 'ignored')
    _remember(line, action='ignore')


def _mark_reviewed(line, status, expense_id=None, income_id=None, reimbursement_id=None):
    line.status = status
    line.expense_id, line.income_id, line.reimbursement_id = expense_id, income_id, reimbursement_id
    line.reviewed_at = datetime.datetime.utcnow()


def _remember(line, action, category=None, sub_category=None, place=None, income_source=None, income_type=None):
    """The latest decision for a merchant wins. Paybacks aren't remembered - each one belongs to a
    specific expense."""
    key = _rule_key(line)
    if key is None:
        return
    rule = ReviewRule.query.filter_by(key=key).first()
    if rule is None:
        rule = ReviewRule(key=key)
        db.session.add(rule)
    rule.action = action
    rule.category, rule.sub_category, rule.place = category, sub_category, place
    rule.income_source, rule.income_type = income_source, income_type


def _account_names():
    names = {a.id: a.name for a in Account.query.all()}
    return {pa.plaid_account_id: names.get(pa.account_id, 'Untracked account') for pa in PlaidAccount.query.all()}


def _expense_label(expense):
    category = expense.category + (f' › {expense.sub_category}' if expense.sub_category and expense.sub_category != 'Misc' else '')
    where = expense.place or expense.description or ''
    unpaid = f'${expense.net_amount:,.2f}' + (f' of ${expense.amount:,.2f}' if expense.reimbursed else '')
    return f'{expense.date:%b %d, %Y} · {category}{f" · {where}" if where else ""} · {unpaid} unpaid'


def _describe_outcome(line):
    if line.status == 'expense':
        expense = db.session.get(Expense, line.expense_id) if line.expense_id else None
        return f'Expense · {expense.category} › {expense.sub_category}' if expense else 'Expense (since deleted)'
    if line.status == 'income':
        income = db.session.get(Income, line.income_id) if line.income_id else None
        if not income:
            return 'Income (since deleted)'
        return f'Income · {INCOME_ENTRY_TYPE_LABELS.get(income.entry_type, income.entry_type)} · {income.source}'
    if line.status == 'payback':
        payback = db.session.get(Reimbursement, line.reimbursement_id) if line.reimbursement_id else None
        expense = db.session.get(Expense, payback.expense_id) if payback else None
        if not expense:
            return 'Payback'
        return f'Paid back toward {expense.place or expense.category} ({expense.date:%b %d, %Y})'
    return 'Ignored'


def _humanize(value):
    """FOOD_AND_DRINK -> Food and Drink"""
    return value.replace('_', ' ').title().replace(' And ', ' and ').replace(' Or ', ' or ')
