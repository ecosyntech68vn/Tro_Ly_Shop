#!/usr/bin/env bash
# ============================================================
# backup.sh — Backup database + gửi file qua Telegram admin
# Chạy thủ công hoặc cron:
#   0 3 * * * /path/to/backup.sh  # mỗi ngày 3h sáng
#
# Hỗ trợ cả SQLite và PostgreSQL (tự động detect).
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Config — load từ .env hoặc env vars
BOT_TOKEN="${BOT_TOKEN:-}"
ADMIN_CHAT_ID="${ADMIN_CHAT_ID:-558789316}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
DB_PATH="${DB_PATH:-bot.db}"
DATABASE_URL="${DATABASE_URL:-}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

mkdir -p "$BACKUP_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

if [ -n "$DATABASE_URL" ]; then
    # PostgreSQL
    echo "[backup] PostgreSQL mode — dumping to $BACKUP_DIR/pg_dump_$TIMESTAMP.sql"
    pg_dump "$DATABASE_URL" > "$BACKUP_DIR/pg_dump_$TIMESTAMP.sql"
    gzip "$BACKUP_DIR/pg_dump_$TIMESTAMP.sql"
    BACKUP_FILE="$BACKUP_DIR/pg_dump_$TIMESTAMP.sql.gz"
else
    # SQLite
    if [ ! -f "$DB_PATH" ]; then
        echo "[backup] No database found at $DB_PATH — skipping"
        exit 0
    fi
    echo "[backup] SQLite mode — copying $DB_PATH"
    # Use VACUUM INTO for consistent snapshot
    sqlite3 "$DB_PATH" "VACUUM INTO '$BACKUP_DIR/bot_$TIMESTAMP.db'"
    gzip "$BACKUP_DIR/bot_$TIMESTAMP.db" 2>/dev/null || true
    # If VACUUM INTO is not available, fallback to cp
    if [ ! -f "$BACKUP_DIR/bot_$TIMESTAMP.db.gz" ]; then
        cp "$DB_PATH" "$BACKUP_DIR/bot_$TIMESTAMP.db"
        gzip "$BACKUP_DIR/bot_$TIMESTAMP.db"
    fi
    BACKUP_FILE="$BACKUP_DIR/bot_$TIMESTAMP.db.gz"
fi

echo "[backup] Created: $BACKUP_FILE ($(du -h "$BACKUP_FILE" | cut -f1))"

# Cleanup old backups
find "$BACKUP_DIR" -name "*.gz" -mtime "+$RETENTION_DAYS" -delete 2>/dev/null || true

# Gửi file qua Telegram (nếu có BOT_TOKEN)
if [ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_CHAT_ID" ]; then
    echo "[backup] Sending to Telegram admin..."
    curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendDocument" \
        -F "chat_id=$ADMIN_CHAT_ID" \
        -F "document=@$BACKUP_FILE" \
        -F "caption=📦 Backup $TIMESTAMP ($(du -h "$BACKUP_FILE" | cut -f1))" \
        > /dev/null
    echo "[backup] Sent to Telegram"
fi

echo "[backup] Done"
