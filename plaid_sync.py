"""Pulls balances (for net worth) and posted transactions (into the review inbox) from every bank
linked with plaid_link.py. Runs on a timer inside the web process, from the dashboard's
"Sync Banks" button, or on demand with `flask --app app plaid-sync`."""
import datetime
import os
import threading
import time

import click
import plaid
from flask import flash, redirect, request, url_for
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.personal_finance_category_version import PersonalFinanceCategoryVersion
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

from app import app, expense_tracker as db, Account, AccountBalance, BankTransaction, PlaidAccount, PlaidItem
from plaid_client import access_tokens, make_client, plaid_error

ACCOUNT_TYPE_FOR_PLAID_TYPE = {
    'depository': 'asset',
    'investment': 'investment',
    'brokerage': 'investment',
    'credit': 'liability',
    'loan': 'liability',
}

NEEDS_RECONNECT = {
    'ITEM_LOGIN_REQUIRED', 'INVALID_CREDENTIALS', 'INVALID_MFA', 'ITEM_LOCKED',
    'ACCESS_NOT_GRANTED', 'ADDITIONAL_CONSENT_REQUIRED', 'USER_SETUP_REQUIRED', 'NO_ACCOUNTS',
}
TRANSIENT = {
    'INSTITUTION_DOWN', 'INSTITUTION_NOT_RESPONDING', 'INSTITUTION_NOT_AVAILABLE',
    'PRODUCT_NOT_READY', 'INTERNAL_SERVER_ERROR', 'PLANNED_MAINTENANCE',
}

_sync_lock = threading.Lock()
# Tokens Plaid couldn't resolve to an Item at all have no PlaidItem row to hold their error.
_label_errors = {}


def run_sync():
    """Sync every linked bank. Returns one summary per bank, or None if a sync is already running."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        return _sync_all()
    finally:
        _sync_lock.release()


def _sync_all():
    client = make_client(os.environ)
    if client is None:
        return []

    resolved = []
    results = []
    for label, token in sorted(access_tokens(os.environ).items()):
        try:
            plaid_item = client.item_get(ItemGetRequest(access_token=token)).item
        except plaid.ApiException as exc:
            err = plaid_error(exc)
            _log_error(label, err)
            _label_errors[label] = _describe_error(err, label)
            results.append({'label': label, 'institution': None, 'error': _label_errors[label]})
            continue
        _label_errors.pop(label, None)
        resolved.append((label, token, plaid_item))

    configured_item_ids = {plaid_item.item_id for _, _, plaid_item in resolved}
    for label, token, plaid_item in resolved:
        results.append(_sync_item(client, label, token, plaid_item, configured_item_ids))
    return results


def _sync_item(client, label, token, plaid_item, configured_item_ids):
    summary = {'label': label, 'institution': plaid_item.get('institution_name'),
               'accounts': 0, 'added': 0, 'updated': 0, 'removed': 0, 'error': None}
    row = _upsert_item_row(label, plaid_item)

    err = None
    try:
        if plaid_item.get('error'):
            # /item/get is authoritative: don't call product endpoints on a broken login.
            err = plaid_item.error.to_dict()
        else:
            synced_at = datetime.datetime.utcnow()
            accounts = client.accounts_get(AccountsGetRequest(access_token=token)).accounts
            plaid_accounts = _sync_accounts(row, accounts, configured_item_ids, synced_at)
            summary['accounts'] = _record_balances(accounts, plaid_accounts)
            if _has_transactions(plaid_item):
                summary.update(_sync_transactions(client, token, row, plaid_accounts))
            row.last_synced_at = synced_at
    except plaid.ApiException as exc:
        db.session.rollback()
        err = plaid_error(exc)
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('Plaid sync failed for %s', label)
        err = {'error_code': type(exc).__name__, 'error_message': str(exc)[:200]}

    row = PlaidItem.query.filter_by(item_id=plaid_item.item_id).one()
    if err:
        _log_error(label, err)
        row.last_error = _describe_error(err, label)
        summary['error'] = row.last_error
    else:
        row.last_error = None
    db.session.commit()
    return summary


def _has_transactions(plaid_item):
    """Logins linked with `plaid_link.py add --investments` never consented to transactions."""
    products = list(plaid_item.get('products') or []) + list(plaid_item.get('billed_products') or [])
    return any(p.value == 'transactions' for p in products)


def _upsert_item_row(label, plaid_item):
    """Committed on its own, so a failure later in the sync still has a row to record its error on."""
    row = PlaidItem.query.filter_by(item_id=plaid_item.item_id).first()
    if row is None:
        # Plaid dates transactions by the bank's local day, which can be a day behind UTC - start a
        # day early rather than miss the day the bank was linked. Overlap with anything already
        # entered by hand just shows up in review to ignore.
        start = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        row = PlaidItem(item_id=plaid_item.item_id, import_from=_midnight(start))
        db.session.add(row)
    row.label = label
    row.institution_name = plaid_item.get('institution_name') or row.institution_name
    db.session.commit()
    return row


def _sync_accounts(row, accounts, configured_item_ids, synced_at):
    """Map each Plaid account to a local Account, creating one the first time it's seen."""
    current_ids = {acct.account_id for acct in accounts}
    mapped = {}
    for acct in accounts:
        pa = (PlaidAccount.query.filter_by(plaid_account_id=acct.account_id).first()
              or _find_reissued_account(row, acct, current_ids, configured_item_ids))
        if pa is None:
            local = Account(
                name=_unique_account_name(row, acct),
                account_type=ACCOUNT_TYPE_FOR_PLAID_TYPE.get(acct.type.value, 'asset'),
            )
            db.session.add(local)
            db.session.flush()
            pa = PlaidAccount(plaid_account_id=acct.account_id, item_id=row.item_id, account_id=local.id)
            db.session.add(pa)
        pa.plaid_account_id = acct.account_id
        pa.item_id = row.item_id
        pa.persistent_account_id = acct.get('persistent_account_id')
        pa.mask = acct.get('mask')
        pa.plaid_type = acct.type.value
        pa.plaid_subtype = _subtype(acct)
        pa.last_seen_at = synced_at
        mapped[acct.account_id] = pa
    return mapped


def _find_reissued_account(row, acct, current_ids, configured_item_ids):
    """Plaid issues a new account_id when an account is renamed at the bank or the bank is
    re-linked after its old connection was removed - reattach to the existing local account
    instead of creating a duplicate (and losing its history)."""
    institution_by_item = {i.item_id: i.institution_name for i in PlaidItem.query.all()}
    candidates = [
        pa for pa in PlaidAccount.query.all()
        if pa.plaid_account_id not in current_ids
        and (pa.item_id == row.item_id or pa.item_id not in configured_item_ids)
    ]
    persistent_id = acct.get('persistent_account_id')
    if persistent_id:
        for pa in candidates:
            if pa.persistent_account_id == persistent_id:
                return pa
    mask = acct.get('mask')
    if mask:
        for pa in candidates:
            same_bank = pa.item_id == row.item_id or institution_by_item.get(pa.item_id) == row.institution_name
            if same_bank and pa.mask == mask and pa.plaid_subtype == _subtype(acct):
                return pa
    return None


def _subtype(acct):
    subtype = acct.get('subtype')
    return subtype.value if subtype else None


def _unique_account_name(row, acct):
    name = acct.name
    institution = row.institution_name
    if institution and not name.lower().startswith(institution.lower()):
        name = f'{institution} {name}'
    if acct.get('mask'):
        name = f'{name} ••{acct.mask}'
    base = name[:90]
    candidate, n = base, 2
    while Account.query.filter_by(name=candidate).first():
        candidate, n = f'{base} ({n})', n + 1
    return candidate


def _record_balances(accounts, plaid_accounts):
    """Upsert this month's balance for every tracked account. Uses /accounts/get's cached
    balances (free, refreshed by Plaid about daily) - plenty for a month-end snapshot."""
    month = datetime.datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    recorded = 0
    for acct in accounts:
        pa = plaid_accounts[acct.account_id]
        balances = acct.balances
        # Credit and loan balances are the amount owed, which is how liabilities are stored here.
        value = balances.get('current')
        if value is None:
            value = balances.get('available')
        if pa.account_id is None or value is None:
            continue
        existing = AccountBalance.query.filter_by(account_id=pa.account_id, date=month).first()
        if existing:
            existing.balance = value
        else:
            db.session.add(AccountBalance(account_id=pa.account_id, date=month, balance=value))
        recorded += 1
    return recorded


def _sync_transactions(client, token, row, plaid_accounts):
    """Queue new posted transactions for review. Lines still waiting for review follow the bank's
    edits and removals; once filed, the user's entry is never changed by the bank."""
    added, modified, removed, cursor, status = _fetch_with_retry(client, token, row.transactions_cursor)
    tracked = _tracked_account_ids(plaid_accounts.values())
    cutoff = row.import_from.date()
    counts = {'added': 0, 'updated': 0, 'removed': 0}

    for t in added:
        if t.account_id in tracked and not t.pending and _transaction_date(t) >= cutoff:
            counts['added'] += _queue(row, t)

    for t in modified:
        waiting = BankTransaction.query.filter_by(plaid_transaction_id=t.transaction_id, status='new').first()
        if waiting:
            _copy_transaction(waiting, t)
            counts['updated'] += 1

    for r in removed:
        counts['removed'] += BankTransaction.query.filter_by(
            plaid_transaction_id=r.transaction_id, status='new').delete()

    # Committed by the caller together with the changes above, so the cursor never runs ahead of the data.
    row.transactions_cursor = cursor
    row.transactions_status = status
    return counts


def backfill(since):
    """Queue transactions dated from `since` up to each bank's current start date, then move the
    start date back. Only that never-imported window is read, so lines the user already ignored
    or filed don't come back. Returns one summary per bank, or None if a sync is running."""
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        client = make_client(os.environ)
        if client is None:
            return []
        results = []
        for label, token in sorted(access_tokens(os.environ).items()):
            summary = {'label': label, 'added': 0, 'error': None, 'note': None}
            results.append(summary)
            try:
                plaid_item = client.item_get(ItemGetRequest(access_token=token)).item
                row = PlaidItem.query.filter_by(item_id=plaid_item.item_id).first()
                if row is None:
                    summary['note'] = 'not synced yet - run plaid-sync first'
                    continue
                old_start = row.import_from.date()
                if since >= old_start:
                    summary['note'] = f'already importing from {old_start}'
                    continue
                if _has_transactions(plaid_item):
                    tracked = _tracked_account_ids(PlaidAccount.query.filter_by(item_id=plaid_item.item_id).all())
                    added, *_ = _fetch_with_retry(client, token, None)  # full history; the saved cursor is untouched
                    for t in added:
                        if t.account_id in tracked and not t.pending and since <= _transaction_date(t) < old_start:
                            summary['added'] += _queue(row, t)
                row.import_from = _midnight(since)
                db.session.commit()
            except plaid.ApiException as exc:
                db.session.rollback()
                err = plaid_error(exc)
                _log_error(label, err)
                summary['error'] = _describe_error(err, label)
        return results
    finally:
        _sync_lock.release()


def _tracked_account_ids(plaid_accounts):
    """Accounts the user deleted locally (account_id NULL) stay out of the inbox too."""
    return {pa.plaid_account_id for pa in plaid_accounts if pa.account_id}


def _queue(row, t):
    if BankTransaction.query.filter_by(plaid_transaction_id=t.transaction_id).first():
        return 0
    line = BankTransaction(plaid_transaction_id=t.transaction_id, item_id=row.item_id, status='new')
    _copy_transaction(line, t)
    db.session.add(line)
    return 1


def _copy_transaction(line, t):
    pfc = t.get('personal_finance_category')
    line.plaid_account_id = t.account_id
    line.date = _midnight(_transaction_date(t))
    line.amount = t.amount
    line.merchant_name = (t.get('merchant_name') or '')[:100] or None
    line.description = (t.get('original_description') or t.get('name') or '')[:200] or None
    line.pfc_primary = pfc.primary if pfc else None
    line.pfc_detailed = pfc.detailed if pfc else None
    line.pfc_confidence = pfc.get('confidence_level') if pfc else None


def _transaction_date(t):
    """When the purchase happened, rather than when it posted."""
    return t.get('authorized_date') or t.date


def _fetch_with_retry(client, token, cursor):
    for _ in range(3):
        try:
            return _fetch_transaction_updates(client, token, cursor)
        except plaid.ApiException as exc:
            # Plaid's data changed mid-pagination: restart from the cursor this fetch began with.
            if plaid_error(exc).get('error_code') != 'TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION':
                raise
    raise RuntimeError('Transactions kept changing during the sync; will retry next time.')


def _fetch_transaction_updates(client, token, cursor):
    """Every page since `cursor`. Nothing is kept until the last page arrives, so an
    interrupted sync starts over from the same cursor instead of skipping transactions."""
    added, modified, removed = [], [], []
    options = TransactionsSyncRequestOptions(
        include_original_description=True,
        personal_finance_category_version=PersonalFinanceCategoryVersion('v2'),
    )
    while True:
        params = {'access_token': token, 'count': 500, 'options': options}
        if cursor:
            params['cursor'] = cursor
        response = client.transactions_sync(TransactionsSyncRequest(**params))
        added += response.added
        modified += response.modified
        removed += response.removed
        cursor = response.next_cursor
        if not response.has_more:
            return added, modified, removed, cursor, response.transactions_update_status.value


def _midnight(day):
    return datetime.datetime(day.year, day.month, day.day)


def _describe_error(err, label):
    code = err.get('error_code') or 'UNKNOWN'
    if code in NEEDS_RECONNECT:
        message = f'Bank login needs attention ({code}). Run: python plaid_link.py update {label}'
    elif code == 'ITEM_NOT_FOUND':
        message = f'This connection no longer exists at Plaid. Run: python plaid_link.py remove {label}, then add it again.'
    elif code in ('INVALID_ACCESS_TOKEN', 'INVALID_API_KEYS'):
        message = f"{code}: the token or API keys don't match PLAID_ENV (sandbox vs production mix-up?)."
    elif err.get('error_type') == 'RATE_LIMIT_EXCEEDED' or code in TRANSIENT:
        message = f'Temporary problem at the bank or Plaid ({code}); will retry on the next sync.'
    else:
        message = f'{code}: {err.get("display_message") or err.get("error_message") or "unexpected error"}'
    return message[:300]


def _log_error(label, err):
    # request_id is what Plaid support needs; tokens never appear in these fields.
    app.logger.warning('Plaid sync error for %s: %s/%s (request_id %s)',
                       label, err.get('error_type'), err.get('error_code'), err.get('request_id'))


def dashboard_status():
    """Per-bank sync state for the dashboard, or None when Plaid isn't configured."""
    if not (os.environ.get('PLAID_CLIENT_ID') and os.environ.get('PLAID_SECRET')):
        return None
    row_by_label = {}
    for row in sorted(PlaidItem.query.all(), key=lambda r: r.last_synced_at or datetime.datetime.min):
        row_by_label[row.label] = row
    items = []
    for label in sorted(access_tokens(os.environ)):
        row = row_by_label.get(label)
        items.append({
            'label': label,
            'institution': row.institution_name if row else None,
            'last_synced_at': row.last_synced_at if row else None,
            'error': _label_errors.get(label) or (row.last_error if row else None),
            'waiting_for_data': bool(row and row.transactions_status == 'NOT_READY'),
            'unshared_accounts': _unshared_account_names(row),
        })
    return {'items': items, 'running': _sync_lock.locked()}


def _unshared_account_names(row):
    """Tracked accounts the bank left out of its last successful sync - un-shared or closed. Their
    last balance keeps counting toward net worth, so the user needs to know."""
    if row is None or row.last_synced_at is None:
        return []
    names = []
    for pa in PlaidAccount.query.filter(PlaidAccount.item_id == row.item_id, PlaidAccount.account_id.isnot(None)):
        if pa.last_seen_at and pa.last_seen_at < row.last_synced_at:
            names.append(db.session.get(Account, pa.account_id).name)
    return sorted(names)


def _sync_in_background():
    with app.app_context():
        try:
            run_sync()
        except Exception:
            app.logger.exception('Plaid sync crashed')


@app.route('/plaid/sync', methods=['POST'])
def plaid_sync_now():
    if _sync_lock.locked():
        flash('A bank sync is already running.')
    else:
        threading.Thread(target=_sync_in_background, daemon=True).start()
        flash('Bank sync started - refresh in a minute to see new balances and transactions to review.')
    return redirect(request.form.get('next') or url_for('index'))


@app.cli.command('plaid-sync')
def plaid_sync_command():
    """Sync balances and transactions from every linked bank now."""
    results = run_sync()
    if results is None:
        click.echo('A sync is already running.')
        return
    if not results:
        click.echo('Nothing to sync: set PLAID_CLIENT_ID / PLAID_SECRET and link a bank with plaid_link.py.')
        return
    for r in results:
        name = f"{r['label']} ({r['institution']})" if r['institution'] else r['label']
        if r['error']:
            click.echo(f"{name}: {r['error']}")
        else:
            click.echo(f"{name}: {r['accounts']} balance(s) recorded; to review +{r['added']} "
                       f"updated {r['updated']} removed {r['removed']}")


@app.cli.command('plaid-backfill')
@click.option('--from', 'since', required=True, type=click.DateTime(formats=['%Y-%m-%d']),
              help='Pull transactions dated on or after this day (YYYY-MM-DD) into the review inbox.')
def plaid_backfill_command(since):
    """Pull older transactions that came before a bank's import start date into the review inbox."""
    results = backfill(since.date())
    if results is None:
        click.echo('A sync is already running - try again in a minute.')
        return
    if not results:
        click.echo('Nothing to backfill: no banks are linked.')
        return
    for r in results:
        detail = r['error'] or r['note'] or f"queued {r['added']} older transaction(s) for review"
        click.echo(f"{r['label']}: {detail}")
    click.echo("Plaid keeps about 90 days of history from when a bank was linked, so earlier dates won't add more.")


def _start_scheduler():
    """Background sync loop inside the web process. Assumes a single gunicorn worker (the
    Dockerfile's default) - each extra worker would run its own loop."""
    raw_hours = os.environ.get('PLAID_SYNC_INTERVAL_HOURS', '12')
    try:
        hours = float(raw_hours)
    except ValueError:
        raise ValueError(f'PLAID_SYNC_INTERVAL_HOURS must be a number, got {raw_hours!r}') from None
    if hours <= 0 or not os.environ.get('PLAID_CLIENT_ID'):
        return
    # With the debug reloader the app is imported by both the file watcher and the server
    # process; only the server process (WERKZEUG_RUN_MAIN) should sync.
    if app.debug and os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        return

    def loop():
        time.sleep(30)
        while True:
            _sync_in_background()
            time.sleep(hours * 3600)

    threading.Thread(target=loop, daemon=True, name='plaid-sync').start()


_start_scheduler()
