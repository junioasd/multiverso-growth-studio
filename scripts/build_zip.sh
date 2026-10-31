#!/usr/bin/env bash
# Gera MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_DOMAIN_POLICY_DURABLE_ANCHOR.zip sanitizado.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="/mnt/user-data/outputs/MULTIVERSO_GROWTH_STUDIO_MASTER_V1_EXPANSION_V6_1_1_ATLAS_PIPELINE1_DOMAIN_POLICY_DURABLE_ANCHOR.zip"
TMP_STAGE="$(mktemp -d)"
DEST="$TMP_STAGE/multiverso_growth_studio"

mkdir -p "$DEST"
cp -r . "$DEST/"

find "$DEST" -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name "*.pyc" -delete
find "$DEST" -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
find "$DEST" -name "*.db" -delete
find "$DEST" -name "*.log" -delete
rm -f "$DEST/.env"

rm -rf "$OUT"
cd "$TMP_STAGE"
zip -rq "$OUT" multiverso_growth_studio -x "*/.git/*"
cd - >/dev/null
rm -rf "$TMP_STAGE"
echo "ZIP gerado em: $OUT"
