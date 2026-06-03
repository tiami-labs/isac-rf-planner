#!/usr/bin/env bash
# Create a repo snapshot zip like rfp_06072025.zip.
#
# Usage:
#   ./scripts/backup_repo.sh                    # rfp_MMDDYYYY.zip
#   ./scripts/backup_repo.sh --phase 1        # rfp_phase1_MMDDYYYY.zip
#   ./scripts/backup_repo.sh --phase 2 --label terrain
#                                           # rfp_phase2-terrain_MMDDYYYY.zip
#   ./scripts/backup_repo.sh --output /path/to/dir
#
# Run at the end of every engineering phase (sample-field, terrain, ISAC, …).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PHASE=""
LABEL=""
OUT_DIR="$ROOT"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phase)
      PHASE="${2:-}"
      shift 2
      ;;
    --label)
      LABEL="${2:-}"
      shift 2
      ;;
    --output)
      OUT_DIR="${2:-}"
      shift 2
      ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

DATE_TAG="$(date +%m%d%Y)"
NAME="rfp_${DATE_TAG}"
if [[ -n "$PHASE" ]]; then
  slug="phase${PHASE}"
  slug="${slug// /-}"
  slug="$(echo "$slug" | tr '[:upper:]' '[:lower:]')"
  if [[ -n "$LABEL" ]]; then
    label_slug="$(echo "$LABEL" | tr '[:upper:]' '[:lower:]' | tr ' ' '-')"
    NAME="rfp_${slug}-${label_slug}_${DATE_TAG}"
  else
    NAME="rfp_${slug}_${DATE_TAG}"
  fi
fi

ZIP_PATH="${OUT_DIR}/${NAME}.zip"
INFO_FILE="${ROOT}/.backup_info.tmp"

{
  echo "backup_name=${NAME}.zip"
  echo "created_utc=$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "created_local=$(date +"%Y-%m-%d %H:%M:%S %Z")"
  echo "repo_root=${ROOT}"
  if [[ -n "$PHASE" ]]; then
    echo "phase=${PHASE}"
  fi
  if [[ -n "$LABEL" ]]; then
    echo "label=${LABEL}"
  fi
  if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "git_head=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
    echo "git_branch=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    echo "git_status=$(git -C "$ROOT" status -sb 2>/dev/null | head -1 || true)"
  fi
} >"$INFO_FILE"

echo "Creating ${ZIP_PATH} ..."

# Match prior manual backups: full tree at repo root, skip other snapshot zips.
zip -r -q "$ZIP_PATH" . \
  -x "./rfp_*.zip" \
  -x "./rf_*.zip" \
  -x "./.git/*" \
  -x "./.git" \
  -x "./node_modules/*" \
  -x "./.venv/*" \
  -x "./venv/*" \
  -x "./.env" \
  -x "./.backup_info.tmp"

zip -q -j "$ZIP_PATH" "$INFO_FILE"
rm -f "$INFO_FILE"

BYTES="$(stat -c%s "$ZIP_PATH" 2>/dev/null || stat -f%z "$ZIP_PATH")"
COUNT="$(unzip -l "$ZIP_PATH" | tail -1 | awk '{print $2}')"
echo "Done: ${ZIP_PATH} (${COUNT} entries, ${BYTES} bytes)"
