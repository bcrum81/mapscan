#!/bin/bash
# End-to-end HTTP test of /settings using a THROWAWAY admin, then clean up.
# Run from the app directory with the API running: bash tests/test_settings_http.sh
set -u
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"
set -a; . ./.env; set +a
B=${BASE_URL:-http://127.0.0.1:8013}
EMAIL="settings-test-$$@example.test"
PW="throwaway-passphrase-$$-xyz"
pass=0; fail=0
chk(){ if [ "$2" = "$3" ]; then echo "  PASS  $1 ($2)"; pass=$((pass+1)); else echo "  FAIL  $1: got $2 want $3"; fail=$((fail+1)); fi; }

# --- create a throwaway admin whose password is already set ---------------
venv/bin/python - "$EMAIL" "$PW" <<'PY'
import asyncio, sys
sys.path.insert(0, ".")
from app import auth, db
async def main():
    await db.init_pool()
    h = await auth.hash_password(sys.argv[2])
    await db.execute(
        "INSERT INTO users (email, email_lower, password_hash, is_admin, is_active,"
        " must_change_password) VALUES ($1,$2,$3,true,true,false)",
        sys.argv[1], sys.argv[1].lower(), h)
    await db.close_pool()
asyncio.run(main())
PY

C=$(mktemp)
code=$(curl -s -c "$C" -o /dev/null -w '%{http_code}' -d "email=$EMAIL&password=$PW" $B/login)
chk "throwaway admin logs in" "$code" "303"

echo
echo "=== /settings renders for an admin ==="
BODY=$(curl -s -b "$C" $B/settings)
chk "GET /settings" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' $B/settings)" "200"
echo "$BODY" | grep -q 'Brave API key'   && { echo "  PASS  brave key field present"; pass=$((pass+1)); } || { echo "  FAIL  no brave field"; fail=$((fail+1)); }
echo "$BODY" | grep -q 'gmail_sa_key'    && { echo "  PASS  gmail service-account field present"; pass=$((pass+1)); } || { echo "  FAIL  no gmail field"; fail=$((fail+1)); }
echo "$BODY" | grep -q 'blocked by DigitalOcean' && { echo "  PASS  SMTP-blocked warning shown"; pass=$((pass+1)); } || { echo "  FAIL  no smtp warning"; fail=$((fail+1)); }
CSRF=$(echo "$BODY" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
[ -n "$CSRF" ] && { echo "  PASS  csrf token present"; pass=$((pass+1)); } || { echo "  FAIL  no csrf"; fail=$((fail+1)); }

echo
echo "=== saving a secret through the form ==="
SECRET="brave-live-test-$$-abcdef"
code=$(curl -s -b "$C" -o /dev/null -w '%{http_code}' \
  -d "csrf_token=$CSRF&brave_search_key=$SECRET&brave_monthly_cap=750" $B/settings)
chk "POST /settings" "$code" "303"
STORED=$(venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from app import db, settings_store
async def m():
    await db.init_pool()
    print(await settings_store.get('brave_search_key'))
    await db.close_pool()
asyncio.run(m())")
chk "secret persisted and decrypts" "$STORED" "$SECRET"

echo
echo "=== the secret must NOT appear in the rendered page ==="
BODY2=$(curl -s -b "$C" $B/settings)
if echo "$BODY2" | grep -q "$SECRET"; then echo "  FAIL  SECRET LEAKED INTO HTML"; fail=$((fail+1));
else echo "  PASS  secret not echoed back to the browser"; pass=$((pass+1)); fi
echo "$BODY2" | grep -q 'stored (database)' && { echo "  PASS  shows 'stored' placeholder instead"; pass=$((pass+1)); } || echo "  note: placeholder text differs"
echo "$BODY2" | grep -q 'value="750"' && { echo "  PASS  non-secret pre-filled"; pass=$((pass+1)); } || { echo "  FAIL  cap not prefilled"; fail=$((fail+1)); }

echo
echo "=== blank secret field means 'leave unchanged', not 'delete' ==="
CSRF2=$(echo "$BODY2" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
curl -s -b "$C" -o /dev/null -d "csrf_token=$CSRF2&brave_search_key=&brave_monthly_cap=750" $B/settings
STILL=$(venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from app import db, settings_store
async def m():
    await db.init_pool(); print(await settings_store.get('brave_search_key')); await db.close_pool()
asyncio.run(m())")
chk "secret survives a blank submit" "$STILL" "$SECRET"

echo
echo "=== CSRF is enforced ==="
code=$(curl -s -b "$C" -o /dev/null -w '%{http_code}' -d "csrf_token=bogus&brave_monthly_cap=1" $B/settings)
chk "bad csrf rejected" "$code" "400"

echo
echo "=== non-admin cannot reach settings ==="
venv/bin/python - "$EMAIL" <<'PY'
import asyncio, sys
sys.path.insert(0, ".")
from app import db
async def main():
    await db.init_pool()
    await db.execute("UPDATE users SET is_admin=false WHERE email_lower=$1", sys.argv[1].lower())
    await db.close_pool()
asyncio.run(main())
PY
chk "demoted user gets 403" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' $B/settings)" "403"

# --- cleanup --------------------------------------------------------------
venv/bin/python - "$EMAIL" <<'PY'
import asyncio, sys
sys.path.insert(0, ".")
from app import db, settings_store
async def main():
    await db.init_pool()
    await db.execute("DELETE FROM users WHERE email_lower=$1", sys.argv[1].lower())
    await settings_store.set_value("brave_search_key", None)
    await settings_store.set_value("brave_monthly_cap", None)
    await db.close_pool()
asyncio.run(main())
PY
rm -f "$C"
echo
echo "cleaned up throwaway admin and test settings"
echo "TALLY pass=$pass fail=$fail"
