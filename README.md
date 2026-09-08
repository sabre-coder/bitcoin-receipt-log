# Bitcoin receipt log

Turn new Bitcoin receipts into a readable Markdown log. A single Python file
checks mempool.space and Blockstream, excludes your starting history, and records
each newly confirmed incoming transaction once.

Useful for a donation page, public funding challenge, or a small project's payment
log. No wallet keys, account, package installation, or API token is required.
The program only reads public explorer APIs and writes local files.

## Quick start

Requires Python 3.9+ on macOS or Linux. Download `receipt_log.py` from this
repository, inspect it, then run:

```sh
python3 receipt_log.py init --address YOUR_BTC_ADDRESS --directory ./receipts
python3 receipt_log.py check --directory ./receipts
python3 receipt_log.py watch --directory ./receipts --interval 120
```

`init` creates a **new** directory containing `baseline.json` and `RECEIPTS.md`.
All confirmed incoming transactions already present become the excluded baseline.
Initialization refuses pending incoming payments so they cannot silently cross
the starting boundary. Keep the generated baseline unchanged.

`check` verifies current receipts and appends new ones to `RECEIPTS.md`. `watch`
repeats the check, including after a temporary network error. Stop with Ctrl-C.
The default interval is 120 seconds; the minimum is 60 seconds.

Each generated entry contains the confirmation block's UTC timestamp, amount in
satoshis and BTC, and a transaction link. Several outputs to the address in one
transaction are combined. Existing prose around the log marker is preserved.

## Verification and limits

- Both explorers must report the same confirmed incoming transactions, amounts,
  block hashes, heights, and timestamps. Their totals must reconcile with all
  incoming outputs. A disagreement produces an inconclusive check without an update.
- Pending payments never enter the log. An entry requires confirmation in a
  block, rather than a configurable minimum confirmation depth.
- The tool measures received outputs, not income provenance or spendable balance.
  It cannot distinguish tips from self-transfers or change returned to the same
  address. Later spending does not erase a recorded receipt.
- A changed or missing baseline transaction stops the check. Entries are records
  of past observations: previously logged transactions are **not automatically
  retracted** if a later chain reorganization removes them. Inspect the explorer
  links when settlement matters.
- Explorer agreement is not local Bitcoin node validation. Both services see the
  address you query and your request's IP address. Availability and their access
  policies apply.
- Every check paginates the address's full confirmed history. This is intended
  for modest donation addresses; it is not an indexer for high-volume wallets.
- A local file lock serializes logger processes, and an atomic replacement
  protects each log update. Avoid editing `RECEIPTS.md` at the same time as a check;
  an editor does not participate in the logger's lock.

Keep the `<!-- received-funds:end -->` marker and generated
`<!-- bitcoin-receipt:TXID -->` suffixes intact. The suffixes prevent duplicate
entries. Only mainnet addresses are accepted; address syntax is screened locally
and the explorers validate the address itself.

Exit codes: `0` for a completed initialization/check, `2` for an inconclusive check
or configuration problem, and `130` when interrupted. A zero exit code does not
mean that any payment arrived; inspect the reported amount.

## Tests

The tests use synthetic transactions, temporary directories, and mocked APIs.
They cover multiple baseline transactions, provider disagreement, pagination,
pending payments, duplicate prevention, concurrent writers, and failed writes.

```sh
python3 -m unittest -v test_receipt_log.py
```

No dependency installation or network access is needed for the tests. CI runs
them on GitHub-hosted Linux and macOS runners.

## Support

This tool is free under the MIT license. It was built with AI assistance for a
challenge to earn 1,280 new satoshis or 0.00040 ETH through useful work. If it helps you, an
optional native Bitcoin tip supports the project:

```text
bc1qgrl4xqvdu03hwacv02jtky4cp6yls0fq05mywx
```

[View the receiving address](https://mempool.space/address/bc1qgrl4xqvdu03hwacv02jtky4cp6yls0fq05mywx).
Funds received before the challenge are not challenge earnings. The address is
for Bitcoin mainnet payments; using the tool does not require sending anything.

Optional native ETH tips are also welcome on **Ethereum mainnet (chain 1)**:

```text
0xb67072a78A7a59B9cD2d50605595d1Da4332b65F
```

[View the Ethereum receiving address](https://etherscan.io/address/0xb67072a78A7a59B9cD2d50605595d1Da4332b65F).
The receipt-logging tool itself monitors Bitcoin. Earlier Ethereum receipts are
excluded from challenge earnings too.
