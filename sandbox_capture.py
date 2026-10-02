#!/usr/bin/env python3
"""Create a USD 39 sandbox purchase, then capture it after buyer approval.

This setup helper never refunds. An interrupted payment POST is not retried.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
import uuid

from paypal import PayPalClient, PayPalError, parse_usd_cents


def save(directory, record):
    temporary = directory / 'purchase.json.tmp'
    temporary.write_text(json.dumps(record, indent=2) + '\n')
    temporary.replace(directory / 'purchase.json')


def create(client, directory, merchant_email, return_url):
    url = urlsplit(return_url)
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost'}
            or url.username or url.password or url.query or url.fragment):
        raise ValueError('Return URL must be the local app URL without query or credentials.')
    if not isinstance(merchant_email, str) or '@' not in merchant_email or len(merchant_email) > 254:
        raise ValueError('Supply the sandbox business account email.')
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    record = {'environment': 'sandbox', 'stage': 'creating', 'amount_minor': 3900,
              'merchant_email': merchant_email, 'order_request_id': str(uuid.uuid4()),
              'capture_request_id': str(uuid.uuid4())}
    payload = {'intent': 'CAPTURE', 'purchase_units': [{
        'reference_id': 'refund-desk-demo', 'description': 'Refund Desk sandbox demonstration',
        'invoice_id': 'refund-desk-' + str(uuid.uuid4()), 'payee': {'email_address': merchant_email},
        'amount': {'currency_code': 'USD', 'value': '39.00'}}],
        'payment_source': {'paypal': {'experience_context': {
            'payment_method_preference': 'IMMEDIATE_PAYMENT_REQUIRED', 'landing_page': 'LOGIN',
            'shipping_preference': 'NO_SHIPPING', 'user_action': 'PAY_NOW',
            'return_url': return_url, 'cancel_url': return_url}}}}
    record['order_payload'] = payload
    save(directory, record)
    try:
        response = client.create_order(payload, record['order_request_id'])
    except PayPalError as exc:
        record.update(stage='create_' + exc.category, error=exc.as_dict())
        save(directory, record)
        raise
    record['order_response'] = response
    record['order_id'] = response['data']['id']
    save(directory, record)
    links = [link.get('href') for link in response['data'].get('links', [])
             if link.get('rel') in {'payer-action', 'approve'}]
    if len(links) != 1:
        raise ValueError('PayPal did not return a unique approval link; inspect the saved response.')
    link = urlsplit(links[0])
    if link.scheme != 'https' or link.hostname != 'www.sandbox.paypal.com' or link.username or link.password:
        raise ValueError('PayPal did not return the expected sandbox buyer site.')
    (directory / 'approval-url.txt').write_text(links[0] + '\n')
    record['stage'] = 'awaiting_buyer'
    save(directory, record)
    return {'stage': record['stage'], 'approval_url_file': str(directory / 'approval-url.txt'),
            'instructions': 'Open the saved URL in your browser and approve with a sandbox Personal buyer. Then run capture.'}


def capture(client, directory):
    descriptor = os.open(directory / '.capture.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ValueError('Capture lock must be a regular file.')
        # Keep concurrent invocations out before reading or updating phase state.
        # A crash releases the lock; the persisted capturing state still blocks a retry.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _capture(client, directory)


def _capture(client, directory):
    record = json.loads((directory / 'purchase.json').read_text())
    if record.get('environment') != 'sandbox' or record.get('stage') != 'awaiting_buyer':
        raise ValueError('This purchase is not awaiting buyer approval. Inspect existing evidence; do not repeat payment POSTs.')
    order = client.get_order(record['order_id'])
    record['order_readback'] = order
    save(directory, record)
    units = order['data'].get('purchase_units', [])
    if (order['data'].get('id') != record['order_id'] or order['data'].get('status') != 'APPROVED'
            or len(units) != 1 or units[0].get('amount') != {'currency_code': 'USD', 'value': '39.00'}
            or units[0].get('payee', {}).get('email_address') != record['merchant_email']):
        raise ValueError('Buyer approval, exact amount or merchant does not match. No capture sent.')
    record['stage'] = 'capturing'
    save(directory, record)
    try:
        response = client.capture_order(record['order_id'], record['capture_request_id'])
    except PayPalError as exc:
        record.update(stage='capture_' + exc.category, error=exc.as_dict())
        save(directory, record)
        raise
    record['capture_response'] = response
    save(directory, record)
    captures = response['data']['purchase_units'][0]['payments']['captures']
    if len(captures) != 1:
        raise ValueError('Capture response is not uniquely identifiable; inspect the saved receipt.')
    capture_id = captures[0]['id']
    record['capture_id'] = capture_id
    record['stage'] = 'capture_pending_verification'
    save(directory, record)
    readback = client.get_capture(capture_id)
    record['capture_readback'] = readback
    save(directory, record)
    data = readback['data']
    if (data.get('id') != capture_id or data.get('status') != 'COMPLETED'
            or data.get('amount', {}).get('currency_code') != 'USD'
            or parse_usd_cents(data['amount']['value']) != 3900
            or data.get('payee', {}).get('email_address') != record['merchant_email']
            or not data.get('payee', {}).get('merchant_id')
            or data.get('supplementary_data', {}).get('related_ids', {}).get('order_id') != record['order_id']):
        raise ValueError('Independent capture details did not match. No refund is authorized.')
    record.update(stage='capture_verified', merchant_id=data['payee']['merchant_id'])
    save(directory, record)
    return {'stage': record['stage'], 'capture_id': capture_id, 'amount': '39.00', 'currency': 'USD',
            'merchant_id': record['merchant_id'], 'merchant_email': record['merchant_email']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'capture'])
    parser.add_argument('--credentials', type=Path, required=True, help='Private 0600 sandbox client credential JSON')
    parser.add_argument('--state-dir', type=Path, required=True, help='New directory for create; same directory for capture')
    parser.add_argument('--merchant-email', help='Sandbox business account email, required for create')
    parser.add_argument('--return-url', default='http://127.0.0.1:8749/checkout-return')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        client = PayPalClient.from_file(args.credentials)
        result = (create(client, args.state_dir.resolve(), args.merchant_email, args.return_url)
                  if args.action == 'create' else capture(client, args.state_dir.resolve()))
        print(json.dumps(result))
    except PayPalError as exc:
        print(json.dumps({'error': exc.as_dict()}))
        return 1
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        print(json.dumps({'error': 'Setup could not complete. Inspect the private purchase.json evidence. No automatic retry.'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
