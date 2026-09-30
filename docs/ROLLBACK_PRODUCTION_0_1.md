# Production rollback baseline: 0.1.0

Created for the production baseline at:

`/home/ubuntu/grannyflat-backups/production-0.1.0-20260926T111426Z`

This procedure is only to be executed after explicit rollback approval.

## Contents

- `full-tree.tar.gz`: complete `/opt/grannyflat-whatsapp-bot` tree, including
  application code, virtual environment, configuration, runtime data, and prior
  deployment artifacts.
- `runtime-data-consistent.zip`: SQLite backup-API snapshots plus knowledge and
  prompt data. These database copies are safe to overlay after restoring the
  full tree.
- `runtime-manifest.json`: file hashes for the consistent runtime backup.
- `SHA256SUMS`: integrity hashes for the baseline artifacts.

## Rollback sequence

1. Verify the backup before stopping anything:

   `cd /home/ubuntu/grannyflat-backups/production-0.1.0-20260926T111426Z && sha256sum -c SHA256SUMS`

2. Set the Twilio maintenance plan, then stop `grannybot`.
3. Rename the current `/opt/grannyflat-whatsapp-bot` directory to a timestamped
   `/opt/grannyflat-whatsapp-bot.failed-*` directory. Do not delete it.
4. Extract `full-tree.tar.gz` under `/opt` with root privileges and preserved
   ownership.
5. Extract `runtime-data-consistent.zip` over
   `/opt/grannyflat-whatsapp-bot/data`, excluding `backup-manifest.json`.
6. Confirm the restored `.env` remains mode `600`, then run SQLite integrity
   checks and the application preflight.
7. Start `grannybot` and verify `/api/status` reports version `0.1.0`.
8. Verify Twilio inbound signature handling, then resume inbound processing.

The rollback must not change the Twilio number, templates, webhook URL, or
credentials. A real customer test message requires separate approval.
