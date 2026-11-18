"""Run the public behavioral probe and write one immutable observation marker."""

import argparse
import hashlib
import importlib.util
import json
import pathlib


def load(path):
    spec = importlib.util.spec_from_file_location("invoice_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DB:
    def __init__(self):
        self.calls = []

    def find_invoice(self, tenant_id, invoice_id):
        self.calls.append([tenant_id, invoice_id])
        return {"tenant_id": tenant_id, "invoice_id": invoice_id}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--fixture", required=True, choices=("a", "b"))
    parser.add_argument("--observations", required=True, type=pathlib.Path)
    args = parser.parse_args()
    if not args.run_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in args.run_id
    ):
        raise SystemExit("invalid run id")
    root = pathlib.Path.cwd()
    module = load(root / "invoice_api.py")
    db, cache = DB(), {}
    first = module.get_invoice(db, cache, "tenant-a", "invoice-17")
    second = module.get_invoice(db, cache, "tenant-b", "invoice-17")
    before_calls = list(db.calls)
    try:
        zero = {"raised": False, "value": module.listing_limit(0)}
    except Exception as exc:
        zero = {"raised": True, "type": type(exc).__name__}
    default_limit = module.listing_limit(None)
    boundary = module.listing_limit(100)
    output = {
        "schema_version": 1,
        "run_id": args.run_id,
        "fixture": args.fixture,
        "cwd": str(root),
        "invoice_calls": before_calls,
        "tenant_results": [first, second],
        "explicit_zero": zero,
        "absent_limit_default": default_limit,
        "valid_boundary": boundary,
    }
    payload = json.dumps(output, sort_keys=True, separators=(",", ":"))
    args.observations.mkdir(parents=True, exist_ok=True)
    marker = args.observations / f"{args.run_id}.json"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(payload + "\n")
    print(payload)
    print(f"marker={marker} sha256={hashlib.sha256(marker.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
