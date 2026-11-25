"""Link banks to the expense tracker through Plaid's hosted page - no Plaid UI inside the app.

  python plaid_link.py add                 open Plaid in your browser; saves the token to .env
  python plaid_link.py add --investments   brokerage/retirement logins that don't offer transactions
  python plaid_link.py update CHASE        reconnect a bank whose login expired, or change which accounts it shares
  python plaid_link.py remove CHASE        disconnect a bank (also stops Plaid billing for it)
  python plaid_link.py list                show linked banks and their health
  python plaid_link.py sandbox-add         sandbox only: link a test bank without the browser

--env-file picks the file to read keys from and save tokens to (default: .env next to this
script), e.g. a separate file holding production keys for the server. Access tokens are
written straight into that file and never printed.
"""
import argparse
import os
import re
import sys
import tempfile
import webbrowser

import plaid
from dotenv import dotenv_values
from plaid.model.country_code import CountryCode
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_hosted_link import LinkTokenCreateHostedLink
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_update import LinkTokenCreateRequestUpdate
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_get_request import LinkTokenGetRequest
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions

from plaid_client import TOKEN_PREFIX, access_tokens, make_client, plaid_error

CLIENT_NAME = 'Expense Tracker'
CLIENT_USER_ID = 'expense-tracker-owner'
SANDBOX_INSTITUTION_ID = 'ins_109508'  # First Platypus Bank


def cmd_add(client, args):
    tokens = access_tokens(args.config)
    if args.name and _label(args.name) in tokens:
        sys.exit(f'{_label(args.name)} is already linked. If its login broke, run: python plaid_link.py update {_label(args.name)}')

    if args.investments:
        products, consented = ['investments'], []
    else:
        # Plaid's recommended net-worth setup: transactions required, liabilities/investments
        # consented up front (billed only if ever used) so adding them later needs no re-consent.
        products, consented = ['transactions'], ['liabilities', 'investments']
    link = client.link_token_create(_link_token_request(
        products=[Products(p) for p in products],
        **({'additional_consented_products': [Products(p) for p in consented]} if consented else {}),
    ))
    session = _wait_for_link(client, link, needs_item=True)
    result = session.results.item_add_results[0]
    _save_item(client, args, tokens, result.public_token, result.institution.institution_id,
               result.institution.name, [(a.name, a.mask, a.type, a.subtype) for a in result.accounts])
    print(f'(Plaid link_session_id {session.link_session_id} - quote this to Plaid support if anything looks wrong.)')


def cmd_sandbox_add(client, args):
    if (args.config.get('PLAID_ENV') or 'sandbox').strip().lower() != 'sandbox':
        sys.exit('sandbox-add only works with PLAID_ENV=sandbox.')
    tokens = access_tokens(args.config)
    created = client.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
        institution_id=SANDBOX_INSTITUTION_ID,
        initial_products=[Products('transactions')],
        options=SandboxPublicTokenCreateRequestOptions(override_username=args.user, override_password='pass_good'),
    ))
    _save_item(client, args, tokens, created.public_token, SANDBOX_INSTITUTION_ID, 'First Platypus Bank', None)


def cmd_update(client, args):
    label = _label(args.label)
    token = _token_or_exit(args, label)
    # Update mode: the existing access_token keeps working afterwards, so there is nothing to
    # exchange or save. Relinking from scratch instead would create a duplicate, separately billed Item.
    # Account selection lets the same flow add an account skipped at link time (e.g. the credit card).
    link = client.link_token_create(_link_token_request(
        access_token=token,
        update=LinkTokenCreateRequestUpdate(account_selection_enabled=True),
    ))
    _wait_for_link(client, link, needs_item=False)
    error = client.item_get(ItemGetRequest(access_token=token)).item.get('error')
    if error:
        sys.exit(f'{label} still reports {error.error_code}. Try again, or check the account at the bank.')
    print(f'{label} is updated. The next sync picks up any accounts you added and catches up on anything missed.')


def cmd_remove(client, args):
    label = _label(args.label)
    token = _token_or_exit(args, label)
    if input(f'Disconnect {label}? Its accounts and history stay in the app. [y/N] ').strip().lower() != 'y':
        return
    try:
        client.item_remove(ItemRemoveRequest(access_token=token))
    except plaid.ApiException as exc:
        if plaid_error(exc).get('error_code') != 'ITEM_NOT_FOUND':
            raise
    _remove_token(args.env_file, label)
    print(f'Disconnected {label} and removed {TOKEN_PREFIX}{label} from {args.env_file}.')
    print('To stop tracking its accounts too, delete them under Manage Accounts in the app.')


def cmd_list(client, args):
    tokens = access_tokens(args.config)
    if not tokens:
        print(f'No banks linked in {args.env_file} yet. Run: python plaid_link.py add')
        return
    for label, token in sorted(tokens.items()):
        try:
            item = client.item_get(ItemGetRequest(access_token=token)).item
        except plaid.ApiException as exc:
            print(f'{label}: {plaid_error(exc).get("error_code")}')
            continue
        error = item.get('error')
        health = f'needs attention: {error.error_code}' if error else 'ok'
        print(f'{label}: {item.get("institution_name") or "unknown bank"} - {health}')


def _link_token_request(**kwargs):
    return LinkTokenCreateRequest(
        client_name=CLIENT_NAME,
        language='en',
        country_codes=[CountryCode('US')],
        user=LinkTokenCreateRequestUser(client_user_id=CLIENT_USER_ID),
        # Plaid's hosted page handles OAuth banks itself (pop-up on desktop), so no redirect URI is needed.
        hosted_link=LinkTokenCreateHostedLink(),
        **kwargs,
    )


def _wait_for_link(client, link, needs_item):
    """Open Plaid's hosted page and wait for the user to finish it. Plaid keeps the session
    result for six hours, so it's read once the user says they're done - no polling."""
    print("Opening Plaid in your browser. If it doesn't open, visit:")
    print(f'  {link.hosted_link_url}')
    webbrowser.open(link.hosted_link_url)
    while True:
        input('\nFinish in the browser, then press Enter here (Ctrl+C to cancel)... ')
        response = client.link_token_get(LinkTokenGetRequest(link_token=link.link_token))
        sessions = [s for s in (response.get('link_sessions') or []) if s.get('finished_at')]
        for session in sessions:
            results = session.get('results')
            linked = bool(results and results.get('item_add_results'))
            if linked or (not needs_item and not session.get('exit')):
                return session
        if sessions:
            error = sessions[-1].exit.get('error') if sessions[-1].get('exit') else None
            reason = f": {error.get('display_message') or error.get('error_code')}" if error else ''
            sys.exit(f'Plaid closed without linking a bank{reason}. Run the command again to retry.')
        print("Plaid hasn't reported a finished session yet - complete the steps in the browser first.")


def _save_item(client, args, tokens, public_token, institution_id, institution_name, accounts):
    duplicate_of = _linked_label_at(client, tokens, institution_id)
    exchange = client.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public_token))
    if duplicate_of and input(
        f'{duplicate_of} is already linked to {institution_name}. Keeping this one too creates a second, '
        'separately billed connection with duplicate accounts. Keep it anyway? [y/N] '
    ).strip().lower() != 'y':
        client.item_remove(ItemRemoveRequest(access_token=exchange.access_token))
        sys.exit(f'Discarded the duplicate. If {duplicate_of} stopped working, run: python plaid_link.py update {duplicate_of}')

    label = _unique_label(_label(args.name or institution_name), tokens)
    _append_token(args.env_file, label, exchange.access_token)
    print(f'\nLinked {institution_name} as {label} (saved {TOKEN_PREFIX}{label} to {args.env_file}).')
    for name, mask, account_type, subtype in accounts or []:
        print(f'  - {name}{f" ••{mask}" if mask else ""} ({account_type}{f"/{subtype}" if subtype else ""})')
    print('Restart the app (or run: flask --app app plaid-sync) to pull its balances and transactions.')


def _linked_label_at(client, tokens, institution_id):
    for label, token in tokens.items():
        try:
            item = client.item_get(ItemGetRequest(access_token=token)).item
        except plaid.ApiException:
            continue
        if item.get('institution_id') == institution_id:
            return label
    return None


def _label(name):
    return re.sub(r'[^A-Z0-9]+', '_', name.upper()).strip('_') or 'BANK'


def _unique_label(label, tokens):
    candidate, n = label, 2
    while candidate in tokens:
        candidate, n = f'{label}_{n}', n + 1
    return candidate


def _token_or_exit(args, label):
    token = access_tokens(args.config).get(label)
    if not token:
        sys.exit(f'No {TOKEN_PREFIX}{label} in {args.env_file}. Run: python plaid_link.py list')
    return token


def _append_token(path, label, token):
    os.chmod(path, 0o600)
    with open(path, 'rb') as f:
        needs_newline = (content := f.read()) and not content.endswith(b'\n')
    with open(path, 'a') as f:
        if needs_newline:
            f.write('\n')
        f.write(f'{TOKEN_PREFIX}{label}={token}\n')


def _remove_token(path, label):
    key = f'{TOKEN_PREFIX}{label}='
    with open(path) as f:
        kept = [line for line in f if not line.lstrip().removeprefix('export ').startswith(key)]
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
    with os.fdopen(fd, 'w') as f:
        f.writelines(kept)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--env-file', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    commands = parser.add_subparsers(dest='command', required=True)

    add = commands.add_parser('add', help='link a new bank login')
    add.add_argument('--name', help='label for its token (default: derived from the bank name)')
    add.add_argument('--investments', action='store_true', help="for brokerage/retirement logins without transactions")
    add.set_defaults(handler=cmd_add)

    update = commands.add_parser('update', help='reconnect a bank, or change which of its accounts are shared')
    update.add_argument('label')
    update.set_defaults(handler=cmd_update)

    remove = commands.add_parser('remove', help='disconnect a bank')
    remove.add_argument('label')
    remove.set_defaults(handler=cmd_remove)

    commands.add_parser('list', help='show linked banks').set_defaults(handler=cmd_list)

    sandbox = commands.add_parser('sandbox-add', help='sandbox only: link a test bank without the browser')
    sandbox.add_argument('--name', help='label for its token (default: FIRST_PLATYPUS_BANK)')
    sandbox.add_argument('--user', default='user_transactions_dynamic',
                         help='sandbox test user, e.g. user_good or user_transactions_dynamic')
    sandbox.set_defaults(handler=cmd_sandbox_add)

    args = parser.parse_args()
    if not os.path.exists(args.env_file):
        sys.exit(f'{args.env_file} not found. Copy .env.example to it and fill in PLAID_CLIENT_ID and '
                 'PLAID_SECRET from https://dashboard.plaid.com/developers/keys')
    args.config = dotenv_values(args.env_file)
    client = make_client(args.config)
    if client is None:
        sys.exit(f'Set PLAID_CLIENT_ID and PLAID_SECRET in {args.env_file} first.')

    try:
        args.handler(client, args)
    except plaid.ApiException as exc:
        err = plaid_error(exc)
        sys.exit(f"Plaid error {err.get('error_type')}/{err.get('error_code')}: "
                 f"{err.get('display_message') or err.get('error_message')} (request_id {err.get('request_id')})")
    except KeyboardInterrupt:
        sys.exit('\nCancelled.')


if __name__ == '__main__':
    main()
