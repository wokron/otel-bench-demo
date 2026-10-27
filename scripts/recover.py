"""Recover all nonterminal experiments while the API is stopped."""
import argparse
import fcntl
import time

from demo_api.app import Target
from demo_api.config import read_config
from demo_api.experiments import Experiments


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = read_config(args.config)
    with open(config.state_path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Experiments(config.state_path, {t.id: Target(t) for t in config.targets})
        try:
            for target in config.targets:
                for row in store.db.execute("SELECT id FROM experiments WHERE target=?", (target.id,)).fetchall():
                    result = store.cancel(target.id, row[0])
                    deadline = time.monotonic() + 60
                    while result["state"] not in {"cancelled", "completed"} and time.monotonic() < deadline:
                        time.sleep(1)
                        result = store.refresh(target.id, row[0])
                    print(row[0], result["state"])
                    if result["state"] not in {"cancelled", "completed"}:
                        raise RuntimeError("Recovery not confirmed; inspect ChaosBlade status/finalizers")
        finally:
            store.db.close()


if __name__ == "__main__":
    main()
