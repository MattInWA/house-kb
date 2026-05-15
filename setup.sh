#!/bin/bash
# setup.sh — First-time setup for House KB
# Run as your app user from the repo directory.
set -e

echo "=== House KB Setup ==="

# Check for libheif (required for HEIC image support)
if ! dpkg -s libheif1 &>/dev/null 2>&1; then
  echo ""
  echo "NOTE: libheif1 not found. HEIC image uploads will fail without it."
  echo "Install with: sudo apt install libheif1"
  echo ""
fi

# Create virtualenv
if [ ! -d venv ]; then
  python3 -m venv venv
  echo "Virtualenv created."
fi

# Install dependencies
venv/bin/pip install -q -r requirements.txt
echo "Dependencies installed."

# Create .env if missing
if [ ! -f .env ]; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  cp .env.template .env
  sed -i "s/replace-with-generated-secret/$SECRET/" .env
  chmod 600 .env
  echo ".env created with generated secret."
  echo ""
  echo "IMPORTANT: Edit .env and set HOUSE_DB and HOUSE_UPLOADS paths before continuing."
  echo "  HOUSE_DB     — path to SQLite database file (will be created)"
  echo "  HOUSE_UPLOADS — path to file upload directory (will be created)"
  exit 0
else
  echo ".env already exists."
fi

# Create upload directory
UPLOAD_DIR=$(grep HOUSE_UPLOADS .env | cut -d= -f2)
if [ -n "$UPLOAD_DIR" ]; then
  mkdir -p "$UPLOAD_DIR"
  echo "Upload directory ready: $UPLOAD_DIR"
fi

# Initialize DB
DB_PATH=$(grep HOUSE_DB .env | cut -d= -f2)
if [ -n "$DB_PATH" ] && [ ! -f "$DB_PATH" ]; then
  venv/bin/python3 init_db.py --db "$DB_PATH"
else
  echo "Database already exists at $DB_PATH — skipping init."
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Start the app:"
echo "     venv/bin/python3 app.py"
echo ""
echo "  2. Or install as a systemd service:"
echo "     Edit house-kb.service — set User and WorkingDirectory"
echo "     sudo cp house-kb.service /etc/systemd/system/"
echo "     sudo systemctl daemon-reload"
echo "     sudo systemctl enable --now house-kb"
echo ""
echo "  3. Optional — reverse proxy via Caddy:"
echo "     http://house.yourdomain.local {"
echo "       reverse_proxy localhost:5055"
echo "     }"
