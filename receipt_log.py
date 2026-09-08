#!/usr/bin/env python3
"""Record newly confirmed Bitcoin receipts in Markdown using two public explorers."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.request import Request, urlopen

PROVIDERS = (
    ("mempool.space", "https://mempool.space/api/address/"),
    ("blockstream.info", "https://blockstream.info/api/address/"),
)
LOG_MARKER = "<!-- received-funds:end -->"

def sats(value):
    if type(value) is not int or value < 0:
        raise ValueError("satoshi counts must be nonnegative integers")
    return value


def parse_snapshot(payload, address):
    try:
        if payload["address"] != address:
            raise ValueError("API returned a different address")
        return {
            "confirmed_received_sats": sats(payload["chain_stats"]["funded_txo_sum"]),
            "pending_received_sats": sats(payload["mempool_stats"]["funded_txo_sum"]),
        }
    except (KeyError, TypeError) as error:
        raise ValueError("API response lacks expected address statistics") from error


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "bitcoin-receipt-log/0.1"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def fetch_snapshot(base_url, address):
    return parse_snapshot(fetch_json(base_url + address), address)


def hash_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid transaction or block ID")
    return value


def fetch_history(base_url, address):
    """Read all pages of confirmed address history, detecting duplicate pages."""
    history, seen = [], set()
    url = base_url + address + "/txs/chain"
    while True:
        page = fetch_json(url)
        if not isinstance(page, list):
            raise ValueError("API transaction history must be a list")
        for transaction in page:
            txid = hash_id(transaction["txid"])
            if txid in seen:
                raise ValueError("API transaction history contains duplicate IDs")
            seen.add(txid)
        history.extend(page)
        if len(page) < 25:
            return history
        url = base_url + address + "/txs/chain/" + page[-1]["txid"]


def incoming_receipts(history, address):
    receipts, seen = {}, set()
    if not isinstance(history, list):
        raise ValueError("API transaction history must be a list")
    try:
        for transaction in history:
            txid = hash_id(transaction["txid"])
            if txid in seen:
                raise ValueError("duplicate transaction ID")
            seen.add(txid)
            status = transaction["status"]
            if type(status["confirmed"]) is not bool:
                raise ValueError("invalid confirmation status")
            if not status["confirmed"]:
                continue
            block_time = sats(status["block_time"])
            block_height = sats(status["block_height"])
            if not block_time or not block_height:
                raise ValueError("invalid confirmation block")
            datetime.fromtimestamp(block_time, timezone.utc)
            block_hash = hash_id(status["block_hash"])
            outputs = transaction["vout"]
            if not isinstance(outputs, list):
                raise ValueError("transaction outputs must be a list")
            amount = 0
            for output in outputs:
                value = sats(output["value"])
                if output.get("scriptpubkey_address") == address:
                    amount += value
            if amount:
                receipts[txid] = {"amount_sats": amount, "block_height": block_height,
                                  "block_hash": block_hash, "block_time": block_time}
    except (KeyError, TypeError, AttributeError, OverflowError, OSError) as error:
        raise ValueError("invalid transaction history schema") from error
    return receipts


def validate_address(address):
    legacy = re.fullmatch(r"[13][1-9A-HJ-NP-Za-km-z]{25,34}", address)
    witness = re.fullmatch(r"bc1[023456789acdefghjklmnpqrstuvwxyz]{11,87}", address)
    if not (legacy or witness):
        raise ValueError("expected a mainnet Bitcoin address (1..., 3..., or lowercase bc1...)")
    return address


def verified_history(address):
    """Reconcile every incoming output with both explorers' aggregate totals."""
    results = []
    for _, base_url in PROVIDERS:
        snapshot = fetch_snapshot(base_url, address)
        receipts = incoming_receipts(fetch_history(base_url, address), address)
        if sum(item["amount_sats"] for item in receipts.values()) != snapshot["confirmed_received_sats"]:
            raise ValueError("history and totals differ; the chain may have changed, retry later")
        results.append((snapshot, receipts))
    if results[0][0]["confirmed_received_sats"] != results[1][0]["confirmed_received_sats"] or results[0][1] != results[1][1]:
        raise ValueError("explorers disagree on confirmed receipts; retry later")
    return results[0][1], [snapshot["pending_received_sats"] for snapshot, _ in results]


def initialize(address, directory):
    address = validate_address(address)
    if directory.exists():
        raise ValueError("directory already exists; choose a new directory to preserve existing files")
    receipts, pending = verified_history(address)
    if any(pending):
        raise ValueError("address has pending incoming outputs; wait for confirmation before setting a baseline")
    baseline = {"schema_version": 1, "address": address,
                "started_at": datetime.now(timezone.utc).isoformat(), "receipts": receipts}
    directory.mkdir(parents=True)
    (directory / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    (directory / "RECEIPTS.md").write_text(
        f"# Bitcoin receipts\n\nAddress: `{address}`\n\n"
        f"Baseline: {sum(item['amount_sats'] for item in receipts.values()):,} sats in "
        f"{len(receipts)} confirmed incoming transaction(s), excluded from this log.\n\n"
        "Entries record the confirmation observed at check time. Explorer agreement is not "
        "independent node validation; old entries are not automatically retracted after a reorganization.\n\n"
        "<!-- received-funds:start -->\n" + LOG_MARKER + "\n", encoding="utf-8")
    print(f"Baseline saved to {directory / 'baseline.json'}")


def new_receipts(baseline, current):
    if baseline.get("schema_version") != 1 or not isinstance(baseline.get("receipts"), dict):
        raise ValueError("invalid baseline schema")
    for txid, receipt in baseline["receipts"].items():
        if current.get(hash_id(txid)) != receipt:
            raise ValueError("a baseline receipt changed or disappeared; inspect the chain before proceeding")
    return {txid: item for txid, item in current.items() if txid not in baseline["receipts"]}


def append_receipts(receipts, directory):
    path = directory / "RECEIPTS.md"
    with (directory / ".receipt-log.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        original = path.read_bytes().decode("utf-8")
        if original.count(LOG_MARKER) != 1:
            raise ValueError("RECEIPTS.md must contain exactly one end marker")
        logged = set(re.findall(r"<!-- bitcoin-receipt:([0-9a-f]{64}) -->", original))
        lines = []
        for txid, receipt in sorted(receipts.items(), key=lambda pair: (pair[1]["block_time"], pair[0])):
            if txid in logged:
                continue
            amount = receipt["amount_sats"]
            timestamp = datetime.fromtimestamp(receipt["block_time"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            btc = f"{amount // 100_000_000}.{amount % 100_000_000:08d}"
            lines.append(f"- {timestamp} (block time): **{amount:,} sats ({btc} BTC)**; "
                         f"[transaction](https://mempool.space/tx/{txid}). <!-- bitcoin-receipt:{txid} -->\n")
        if not lines:
            return 0
        updated = original.replace(LOG_MARKER, "".join(lines) + LOG_MARKER)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".receipts-", delete=False) as file:
                temporary = Path(file.name)
                file.write(updated.encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return len(lines)


def check(directory):
    baseline = json.loads((directory / "baseline.json").read_text(encoding="utf-8"))
    address = validate_address(baseline["address"])
    current, pending = verified_history(address)
    receipts = new_receipts(baseline, current)
    added = append_receipts(receipts, directory)
    print(f"{address}: {sum(item['amount_sats'] for item in receipts.values()):,} new confirmed sats; "
          f"{added} log entries added; pending sats by explorer: {pending}")


def interval_seconds(value):
    value = int(value)
    if value < 60:
        raise argparse.ArgumentTypeError("interval must be at least 60 seconds")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="capture the current confirmed history as the excluded baseline")
    init.add_argument("--address", required=True)
    init.add_argument("--directory", type=Path, required=True)
    for name in ("check", "watch"):
        command = sub.add_parser(name)
        command.add_argument("--directory", type=Path, required=True)
        if name == "watch":
            command.add_argument("--interval", type=interval_seconds, default=120)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            initialize(args.address, args.directory)
            return 0
        while True:
            try:
                check(args.directory)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"INCONCLUSIVE: {error}", file=sys.stderr)
                if args.command != "watch":
                    return 2
            if args.command != "watch":
                return 0
            sys.stdout.flush()
            time.sleep(args.interval)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"INCONCLUSIVE: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
