#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${ROOT_DIR}/database.db"
BACKUP_DIR="${ROOT_DIR}/backups"

mkdir -p "${BACKUP_DIR}"

if [[ ! -f "${DB_PATH}" ]]; then
  echo "Database file not found: ${DB_PATH}" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_path="${BACKUP_DIR}/database_${timestamp}.db"
cp "${DB_PATH}" "${backup_path}"

echo "Backup created: ${backup_path}"
