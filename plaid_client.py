import json

import plaid
from plaid.api import plaid_api

ENVIRONMENTS = {'sandbox': plaid.Environment.Sandbox, 'production': plaid.Environment.Production}
TOKEN_PREFIX = 'PLAID_ACCESS_TOKEN_'


def make_client(config):
    """Plaid API client from PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ENV in `config`, or None if keys are missing."""
    client_id, secret = config.get('PLAID_CLIENT_ID'), config.get('PLAID_SECRET')
    if not client_id or not secret:
        return None
    env_name = (config.get('PLAID_ENV') or 'sandbox').strip().lower()
    if env_name not in ENVIRONMENTS:
        raise ValueError(f'PLAID_ENV must be one of {sorted(ENVIRONMENTS)}, got {env_name!r}')
    configuration = plaid.Configuration(
        host=ENVIRONMENTS[env_name],
        api_key={'clientId': client_id, 'secret': secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def access_tokens(config):
    """{label: access_token} for every PLAID_ACCESS_TOKEN_<LABEL> entry - one per linked bank login."""
    return {
        key[len(TOKEN_PREFIX):]: value
        for key, value in config.items()
        if key.startswith(TOKEN_PREFIX) and value
    }


def plaid_error(exc):
    """The error body of a failed Plaid call: error_type, error_code, request_id, etc. Never contains tokens."""
    try:
        body = json.loads(exc.body)
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict):
        return {'error_type': 'API_ERROR', 'error_code': f'HTTP_{exc.status}', 'error_message': str(exc.reason)}
    return body
