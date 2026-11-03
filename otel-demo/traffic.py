"""Generate bounded catalog traffic; does not run automatically during report collection."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check import forward


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, default=120)
    parser.add_argument('--interval', type=float, default=0.5)
    args = parser.parse_args()
    if not 1 <= args.duration <= 600 or not 0.1 <= args.interval <= 30:
        parser.error('duration: 1..600 seconds; interval: 0.1..30 seconds')
    counts = Counter()
    with forward(os.environ.get('PROFILE', 'minikube'), os.environ.get('NAMESPACE', 'otel-demo'), 'product-catalog', 8080) as port:
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            result = subprocess.run([os.environ.get('GRPCURL', 'grpcurl'), '-plaintext', '-max-time', '5',
                                     '-d', '{}', f'127.0.0.1:{port}',
                                     'oteldemo.ProductCatalogService/ListProducts'], capture_output=True, text=True)
            counts['success' if result.returncode == 0 else 'failed'] += 1
            if result.returncode:
                print(result.stderr[:300], file=sys.stderr, flush=True)
            time.sleep(args.interval)
    print(json.dumps(dict(counts)))
    if counts['failed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
