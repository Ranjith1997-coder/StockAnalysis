#!/usr/bin/env bash
#
# Pull production state (paper trades, PINN models, recorded option chains)
# from the server into ./backups/<timestamp>/ on this laptop.
#
# Usage:
#   bash scripts/backup_server_data.sh [destination_root]
#   DEPLOY_SERVER=hacker@100.92.21.31 BACKUP_KEEP=30 bash scripts/backup_server_data.sh
#
# Suggested schedule (laptop, after market close):
#   15 16 * * 1-5  cd ~/Ranjith/StockAnalysis && bash scripts/backup_server_data.sh
#
set -euo pipefail

SERVER="${DEPLOY_SERVER:-hacker@100.92.21.31}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_ROOT="${1:-$ROOT/backups}"
KEEP="${BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"
DEST="$DEST_ROOT/$STAMP"

mkdir -p "$DEST"
RSYNC=(rsync -az --no-perms --no-owner --no-group)

echo "Backing up $SERVER -> $DEST"

"${RSYNC[@]}" "$SERVER:StockAnalysis/data/paper_trades.db" "$DEST/" \
  || echo "WARN: paper_trades.db not available"
"${RSYNC[@]}" "$SERVER:StockAnalysis/data/pinn_models/" "$DEST/pinn_models/" \
  || echo "WARN: pinn_models not available"
"${RSYNC[@]}" "$SERVER:StockAnalysis/data/options_history/" "$DEST/options_history/" \
  2>/dev/null || echo "WARN: options_history not present yet (Milestone M1)"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$SERVER" \
  "cd StockAnalysis && echo commit=\$(git rev-parse HEAD) && date -Is" \
  > "$DEST/server_state.txt" || echo "WARN: could not record server state"

# Keep the newest $KEEP snapshots.
ls -1dt "$DEST_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
  rm -rf "$old"
done

du -sh "$DEST" 2>/dev/null || true
echo "Backup complete."
