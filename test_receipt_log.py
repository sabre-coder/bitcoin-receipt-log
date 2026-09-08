import contextlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import receipt_log as log


ADDRESS = "bc1qgrl4xqvdu03hwacv02jtky4cp6yls0fq05mywx"


def transaction(number, amounts, confirmed=True):
    return {"txid": f"{number:064x}", "status": {
        "confirmed": confirmed, "block_time": 1788860000,
        "block_height": 900000, "block_hash": "a" * 64},
        "vout": [{"scriptpubkey_address": ADDRESS, "value": amount} for amount in amounts]}


class ReceiptLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "receipts"
        self.old = log.incoming_receipts([transaction(1, [700]), transaction(2, [300])], ADDRESS)
        self.new = log.incoming_receipts([transaction(3, [500, 780])], ADDRESS)

    def initialize(self, receipts=None):
        if receipts is None:
            receipts = self.old
        with patch.object(log, "verified_history", return_value=(receipts, [0, 0])), \
                contextlib.redirect_stdout(io.StringIO()):
            log.initialize(ADDRESS, self.directory)

    def test_complete_init_check_flow_preserves_text_and_deduplicates(self):
        self.initialize()
        path = self.directory / "RECEIPTS.md"
        original = path.read_text() + "\nMy project notes.\n"
        path.write_text(original)
        with patch.object(log, "verified_history", return_value=({**self.old, **self.new}, [900, 900])), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(log.main(["check", "--directory", str(self.directory)]), 0)
            self.assertEqual(log.main(["check", "--directory", str(self.directory)]), 0)
        updated = path.read_text()
        self.assertEqual(updated.count("<!-- bitcoin-receipt:"), 1)
        self.assertIn("1,280 sats (0.00001280 BTC)", updated)
        self.assertIn("1,280 new confirmed sats", output.getvalue())
        self.assertEqual("".join(line for line in updated.splitlines(keepends=True)
                                 if "<!-- bitcoin-receipt:" not in line), original)

    def test_empty_baseline_is_supported(self):
        self.initialize({})
        baseline = json.loads((self.directory / "baseline.json").read_text())
        self.assertEqual(log.new_receipts(baseline, self.new), self.new)

    def test_init_refuses_pending_or_existing_directory(self):
        with patch.object(log, "verified_history", return_value=(self.old, [1, 0])):
            with self.assertRaises(ValueError):
                log.initialize(ADDRESS, self.directory)
        self.assertFalse(self.directory.exists())
        self.initialize()
        original = (self.directory / "baseline.json").read_bytes()
        with self.assertRaises(ValueError):
            log.initialize(ADDRESS, self.directory)
        self.assertEqual((self.directory / "baseline.json").read_bytes(), original)

    def test_address_rejects_network_and_path_characters(self):
        for address in ("https://example.com", ADDRESS + "/txs", "tb1q" + "a" * 38, "", "../baseline"):
            with self.assertRaises(ValueError):
                log.validate_address(address)

    def test_multiple_baseline_receipts_are_excluded_and_reorg_is_inconclusive(self):
        baseline = {"schema_version": 1, "receipts": self.old}
        self.assertEqual(log.new_receipts(baseline, {**self.old, **self.new}), self.new)
        for key in ("amount_sats", "block_height", "block_time", "block_hash"):
            changed = deepcopy(self.old)
            changed[next(iter(changed))][key] = "changed"
            with self.assertRaises(ValueError):
                log.new_receipts(baseline, changed)
        with self.assertRaises(ValueError):
            log.new_receipts(baseline, {})

    def test_pending_outputs_are_excluded_and_other_addresses_are_ignored(self):
        tx = transaction(3, [500, 780])
        tx["vout"].append({"scriptpubkey_address": "other", "value": 999999})
        self.assertEqual(log.incoming_receipts([tx, transaction(4, [5000], False)], ADDRESS), self.new)

    def test_provider_agreement_and_total_reconciliation(self):
        history = [transaction(3, [500, 780])]
        snapshots = [{"confirmed_received_sats": 1280, "pending_received_sats": 0}] * 2
        with patch.object(log, "fetch_snapshot", side_effect=snapshots), \
                patch.object(log, "fetch_history", side_effect=[history, history]):
            self.assertEqual(log.verified_history(ADDRESS), (self.new, [0, 0]))
        with patch.object(log, "fetch_snapshot", return_value=snapshots[0]), \
                patch.object(log, "fetch_history", return_value=[transaction(3, [1279])]):
            with self.assertRaises(ValueError):
                log.verified_history(ADDRESS)
        other = deepcopy(history)
        other[0]["status"]["block_hash"] = "b" * 64
        with patch.object(log, "fetch_snapshot", side_effect=snapshots), \
                patch.object(log, "fetch_history", side_effect=[history, other]):
            with self.assertRaises(ValueError):
                log.verified_history(ADDRESS)

    def test_invalid_or_duplicate_receipts_are_rejected(self):
        for amount in (True, -1, "100"):
            with self.assertRaises(ValueError):
                log.incoming_receipts([transaction(3, [amount])], ADDRESS)
        tx = transaction(3, [100])
        with self.assertRaises(ValueError):
            log.incoming_receipts([tx, tx], ADDRESS)
        tx["txid"] = "bad"
        with self.assertRaises(ValueError):
            log.incoming_receipts([tx], ADDRESS)

    def test_pagination_and_duplicate_page_detection(self):
        page = [transaction(number, [1]) for number in range(25)]
        with patch.object(log, "fetch_json", side_effect=[page, []]) as fetch:
            self.assertEqual(len(log.fetch_history("https://example.test/", ADDRESS)), 25)
        self.assertTrue(fetch.call_args.args[0].endswith(page[-1]["txid"]))
        with patch.object(log, "fetch_json", side_effect=[page, page]):
            with self.assertRaises(ValueError):
                log.fetch_history("https://example.test/", ADDRESS)

    def test_concurrent_writers_keep_both_receipts_once(self):
        self.initialize()
        other = log.incoming_receipts([transaction(4, [2000])], ADDRESS)
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(log.append_receipts, receipt, self.directory)
                     for receipt in (self.new, other, self.new, other)]
            self.assertEqual(sum(task.result() for task in tasks), 2)
        self.assertEqual((self.directory / "RECEIPTS.md").read_text().count("<!-- bitcoin-receipt:"), 2)

    def test_write_failure_and_missing_marker_preserve_file(self):
        self.initialize()
        path = self.directory / "RECEIPTS.md"
        original = path.read_bytes()
        with patch.object(log.os, "replace", side_effect=PermissionError("read-only")):
            with self.assertRaises(PermissionError):
                log.append_receipts(self.new, self.directory)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.directory.glob(".receipts-*")))
        path.write_text("User content, no marker.")
        with self.assertRaises(ValueError):
            log.append_receipts(self.new, self.directory)
        self.assertEqual(path.read_text(), "User content, no marker.")

    def test_watch_retries_inconclusive_check_and_stops_on_interrupt(self):
        with patch.object(log, "check", side_effect=[ValueError("providers differ"), None]) as check, \
                patch.object(log.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(log.main(["watch", "--directory", str(self.directory)]), 130)
        self.assertEqual(check.call_count, 2)

    def test_check_failure_has_distinct_exit_code(self):
        with patch.object(log, "check", side_effect=OSError("offline")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(log.main(["check", "--directory", str(self.directory)]), 2)


if __name__ == "__main__":
    unittest.main()
