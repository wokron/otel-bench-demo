"""Real baseline/fault/recovery checks using the public API and independent demo RPCs."""
import argparse
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import httpx

from demo_api.providers.kubernetes import Kubectl
from demo_api.providers.transport import endpoint

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ['business.payment.failure', 'business.cart.failure', 'business.payment.unavailable']
CARD = {'creditCardNumber': '4111111111111111', 'creditCardCvv': 123,
        'creditCardExpirationYear': 2035, 'creditCardExpirationMonth': 12}


def wait_state(client, path, desired, timeout=240):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        result = response.json()
        if result['state'] == desired:
            return result
        if desired == 'running' and result['state'] in {'failed', 'cancelled', 'completed'}:
            raise RuntimeError(f'Fault did not become running: {result}')
        time.sleep(1)
    raise RuntimeError(f'Timed out waiting for {desired}: {result}')


def run_case(client, prefix, action, probe, control, samples, save, expected_namespace=None):
    record = {'action': action, 'started_at': datetime.now(timezone.utc).isoformat()}
    save(record)
    record['baseline'] = [probe() for _ in range(samples)]
    save(record)
    if any(r['exit_code'] for r in record['baseline']):
        raise RuntimeError(f'Baseline failed for {action}: {record["baseline"]}')
    service = 'cart' if action == 'business.cart.failure' else 'payment'
    body = {'action': action, 'service': service, 'parameters': {} if action.endswith('unavailable') else {'percent': 100},
            'duration_seconds': 300, 'dry_run': True}
    response = client.post(prefix, json=body)
    response.raise_for_status()
    record['preview'] = response.json()
    plan = record['preview']['plan']
    inner = plan.get('plan', plan)
    if expected_namespace is not None and inner.get('namespace') != expected_namespace:
        raise RuntimeError('API fault target and independent RPC probe namespace differ')
    save(record)
    body['dry_run'] = False
    key = 'business-validation-' + uuid.uuid4().hex
    record['idempotency_key'] = key
    save(record)
    # A lost response can follow a successful write: retry the SAME key to recover the ID.
    try:
        response = client.post(prefix, json=body, headers={'Idempotency-Key': key})
    except httpx.TransportError:
        response = client.post(prefix, json=body, headers={'Idempotency-Key': key})
    if response.status_code == 502:
        response = client.post(prefix, json=body, headers={'Idempotency-Key': key})
    response.raise_for_status()
    created = response.json()
    record['experiment_id'] = created['id']
    path = prefix + '/' + created['id']
    try:
        save(record)
        record['running'] = wait_state(client, path, 'running')
        record['running_confirmed_at'] = datetime.now(timezone.utc).isoformat()
        record['during'] = [probe() for _ in range(samples)]
        record['control'] = control()
        save(record)
        if not all(r['exit_code'] for r in record['during']):
            raise RuntimeError(f'Expected all business requests to fail: {record["during"]}')
        if record['control']['exit_code']:
            raise RuntimeError('Unaffected control request failed')
    finally:
        response = client.delete(path)
        response.raise_for_status()
        record['cleanup'] = wait_state(client, path, 'cancelled')
        record['cleanup_confirmed_at'] = datetime.now(timezone.utc).isoformat()
        save(record)
    record['recovery'] = [probe() for _ in range(samples)]
    save(record)
    if any(r['exit_code'] for r in record['recovery']):
        raise RuntimeError(f'Business recovery failed: {record["recovery"]}')
    record['passed'] = True
    record['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(record)
    print(f'PASS {action}: baseline {samples}/{samples} OK, fault {samples}/{samples} failed, control OK, recovery {samples}/{samples} OK', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--target', default='otel-full-existing')
    parser.add_argument('--context', default='minikube')
    parser.add_argument('--namespace', default='default')
    parser.add_argument('--action', choices=ACTIONS, action='append')
    parser.add_argument('--samples', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--grpcurl', default=shutil.which('grpcurl') or str(ROOT / '.cache/tools/grpcurl'))
    parser.add_argument('--output', default='.reports/business-validation.json')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        parser.error('--execute is required: changes real business behavior and sends synthetic demo RPCs')
    report = {'target_id': args.target, 'context': args.context, 'namespace': args.namespace, 'cases': []}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    def persist(record):
        if record is not None and not any(r is record for r in report['cases']):
            report['cases'].append(record)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(output)
    token = os.environ.get('DEMO_API_TOKEN')
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    kube = Kubectl({'context': args.context, 'namespace': args.namespace})
    with contextlib.ExitStack() as stack:
        client = stack.enter_context(httpx.Client(base_url=args.url, headers=headers, timeout=120, trust_env=False))
        prefix = f'/v1/targets/{args.target}/experiments'
        urls = {s: stack.enter_context(endpoint({'service': s, 'port': 8080}, kube)).removeprefix('http://')
                for s in ('payment', 'cart', 'checkout')}
        user = 'demo-api-validation-' + uuid.uuid4().hex
        def rpc(service, method, body):
            command = [args.grpcurl, '-plaintext', '-max-time', '20', '-import-path', str(ROOT / 'examples/probes'),
                       '-proto', 'demo.proto', '-H', 'baggage: synthetic_request=true', '-d', json.dumps(body),
                       urls[service], 'oteldemo.' + method]
            started = time.monotonic()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            return {'service': service, 'method': method, 'exit_code': result.returncode,
                    'response': result.stdout.strip(), 'error': result.stderr.strip(),
                    'duration_seconds': round(time.monotonic() - started, 3)}
        def payment():
            return rpc('payment', 'PaymentService/Charge', {'amount': {'currencyCode': 'USD', 'units': 1}, 'creditCard': CARD})
        def cart():
            return rpc('cart', 'CartService/EmptyCart', {'userId': user})
        def cart_control():
            return rpc('cart', 'CartService/GetCart', {'userId': user})
        def checkout():
            added = rpc('cart', 'CartService/AddItem', {'userId': user, 'item': {'productId': 'OLJCESPC7Z', 'quantity': 1}})
            if added['exit_code']:
                raise RuntimeError(f'Checkout preparation AddItem failed: {added}')
            return rpc('checkout', 'CheckoutService/PlaceOrder', {'userId': user, 'userCurrency': 'USD',
                'address': {'streetAddress': '1 Test Street', 'city': 'Mountain View', 'state': 'CA',
                            'country': 'United States', 'zipCode': '94043'},
                'email': 'demo-api-test@example.invalid', 'creditCard': CARD})
        probes = {ACTIONS[0]: (payment, cart_control), ACTIONS[1]: (cart, cart_control), ACTIONS[2]: (checkout, payment)}
        try:
            for action in args.action or ACTIONS:
                run_case(client, prefix, action, *probes[action], args.samples, persist,
                         expected_namespace=args.namespace)
        finally:
            # Remove only this run's disposable user's cart; fault cleanup is in run_case.
            result = cart()
            report['test_user_cleanup'] = result
            persist(None)
            if result['exit_code']:
                raise RuntimeError(f'Test user cleanup failed: {result}')
    print(f'Evidence written to {output}', flush=True)


if __name__ == '__main__':
    main()
