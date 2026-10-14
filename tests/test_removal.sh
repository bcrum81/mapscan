#!/bin/bash
# Removal controls, end to end, with a throwaway admin. Cleans up after itself.
set -u
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"; cd "$APP_DIR"; set -a; . ./.env; set +a
B=${BASE_URL:-http://127.0.0.1:8013}
EMAIL="rm-test-$$@example.test"; PW="throwaway-passphrase-$$-xyz"
pass=0; fail=0
chk(){ if [ "$2" = "$3" ]; then echo "  PASS  $1 ($2)"; pass=$((pass+1)); else echo "  FAIL  $1: got $2 want $3"; fail=$((fail+1)); fi; }
q(){ psql "$DATABASE_URL" -Atc "$1" 2>/dev/null; }

venv/bin/python - "$EMAIL" "$PW" <<'PY'
import asyncio, sys; sys.path.insert(0, ".")
from app import auth, db
async def m():
    await db.init_pool()
    h = await auth.hash_password(sys.argv[2])
    await db.execute("INSERT INTO users (email,email_lower,password_hash,is_admin,"
                     "is_active,must_change_password) VALUES ($1,$2,$3,true,true,false)",
                     sys.argv[1], sys.argv[1].lower(), h)
    await db.close_pool()
asyncio.run(m())
PY
C=$(mktemp)
curl -s -c "$C" -o /dev/null -d "email=$EMAIL&password=$PW" $B/login
CSRF=$(curl -s -b "$C" $B/sellers | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')

# --- synthetic fixtures: this test must never touch real listings ----------
TAGD="removal-test-$$.example"
q "INSERT INTO sellers (domain) VALUES ('$TAGD')" >/dev/null
TSID=$(q "SELECT id FROM sellers WHERE domain='$TAGD'")
PID=$(q "SELECT id FROM products WHERE active ORDER BY id LIMIT 1")
mk_listing(){  # $1 = url suffix -> echoes the new listing id
  q "INSERT INTO listings (product_id, seller_id, url, status, match_score, below_map_streak)
     VALUES ($PID, $TSID, 'https://$TAGD/p/$1', 'confirmed', 100, 2) RETURNING id"
}
L_UNTRACK=$(mk_listing untrack)
L_DELETE=$(mk_listing delete)
for L in $L_UNTRACK $L_DELETE; do
  q "INSERT INTO price_observations (listing_id, price, extraction_tier, below_map)
     VALUES ($L, 10.00, 'test', true)" >/dev/null
  q "INSERT INTO violations (listing_id, status, map_at_open, opening_price, lowest_price)
     VALUES ($L, 'open', 20.00, 10.00, 10.00)" >/dev/null
done
# a real evidence artifact on disk for the delete test
mkdir -p ${EVIDENCE_DIR:-./evidence}/testfixture
echo test | gzip > "${EVIDENCE_DIR:-./evidence}/testfixture/rm$$.html.gz"
q "INSERT INTO evidence (listing_id, url, html_path, observed_price, map_price)
   VALUES ($L_DELETE, 'https://$TAGD/p/delete', 'testfixture/rm$$.html.gz', 10.00, 20.00)" >/dev/null
echo "  fixtures: seller $TSID, listings $L_UNTRACK (untrack) / $L_DELETE (delete)"

echo "=== sellers page renders ==="
chk "GET /sellers" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' $B/sellers)" "200"

echo
echo "=== untrack keeps history, closes the violation ==="
LID=$L_UNTRACK
OBS_BEFORE=$(q "SELECT count(*) FROM price_observations WHERE listing_id=$LID")
curl -s -b "$C" -o /dev/null -d "csrf_token=$CSRF" $B/listings/$LID/untrack
chk "status now rejected" "$(q "SELECT status FROM listings WHERE id=$LID")" "rejected"
chk "retired_at set" "$(q "SELECT retired_at IS NOT NULL FROM listings WHERE id=$LID")" "t"
chk "price history KEPT" "$(q "SELECT count(*) FROM price_observations WHERE listing_id=$LID")" "$OBS_BEFORE"
chk "open violation closed" "$(q "SELECT count(*) FROM violations WHERE listing_id=$LID AND status='open'")" "0"
chk "violation history kept" "$(q "SELECT count(*)>0 FROM violations WHERE listing_id=$LID")" "t"

echo
echo "=== untracked listings are not re-checked ==="
DUE=$(q "SELECT count(*) FROM listings l JOIN products p ON p.id=l.product_id
         JOIN sellers s ON s.id=l.seller_id
         WHERE l.status='confirmed' AND l.retired_at IS NULL AND p.active
           AND NOT s.robots_disallowed AND NOT s.excluded AND l.id=$LID")
chk "excluded from the recheck query" "$DUE" "0"

echo
echo "=== delete removes rows AND evidence files ==="
LID2=$L_DELETE
EVF=$(q "SELECT html_path FROM evidence WHERE listing_id=$LID2 LIMIT 1")
echo "        evidence file: $EVF"
[ -f "${EVIDENCE_DIR:-./evidence}/$EVF" ] && { echo "  PASS  file exists before delete"; pass=$((pass+1)); } || { echo "  FAIL  file missing before delete"; fail=$((fail+1)); }
curl -s -b "$C" -o /dev/null -d "csrf_token=$CSRF" $B/listings/$LID2/delete
chk "listing row gone" "$(q "SELECT count(*) FROM listings WHERE id=$LID2")" "0"
chk "observations cascaded" "$(q "SELECT count(*) FROM price_observations WHERE listing_id=$LID2")" "0"
chk "violations cascaded" "$(q "SELECT count(*) FROM violations WHERE listing_id=$LID2")" "0"
chk "evidence rows cascaded" "$(q "SELECT count(*) FROM evidence WHERE listing_id=$LID2")" "0"
[ -f "${EVIDENCE_DIR:-./evidence}/$EVF" ] && { echo "  FAIL  evidence FILE still on disk (orphaned)"; fail=$((fail+1)); } || { echo "  PASS  evidence file deleted from disk"; pass=$((pass+1)); }

echo
echo "=== exclude blocks future scanning ==="
SID=$TSID
curl -s -b "$C" -o /dev/null -d "csrf_token=$CSRF&reason=test" $B/sellers/$SID/exclude
chk "marked excluded" "$(q "SELECT excluded FROM sellers WHERE id=$SID")" "t"
chk "reason recorded" "$(q "SELECT excluded_reason FROM sellers WHERE id=$SID")" "test"
chk "re-enable works" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' -d "csrf_token=$CSRF" $B/sellers/$SID/include)" "303"
chk "no longer excluded" "$(q "SELECT excluded FROM sellers WHERE id=$SID")" "f"

echo
echo "=== CSRF enforced on destructive routes ==="
for r in "listings/1/delete" "listings/1/untrack" "sellers/$SID/exclude"; do
  chk "bad csrf on /$r" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' -d "csrf_token=bogus" $B/$r)" "400"
done

echo
echo "=== non-admin cannot exclude a seller ==="
venv/bin/python - "$EMAIL" <<'PY'
import asyncio, sys; sys.path.insert(0, ".")
from app import db
async def m():
    await db.init_pool()
    await db.execute("UPDATE users SET is_admin=false WHERE email_lower=$1", sys.argv[1].lower())
    await db.close_pool()
asyncio.run(m())
PY
chk "demoted user gets 403" "$(curl -s -b "$C" -o /dev/null -w '%{http_code}' -d "csrf_token=$CSRF" $B/sellers/$SID/exclude)" "403"

venv/bin/python - "$EMAIL" <<'PY'
import asyncio, sys; sys.path.insert(0, ".")
from app import db
async def m():
    await db.init_pool()
    await db.execute("DELETE FROM users WHERE email_lower=$1", sys.argv[1].lower())
    await db.close_pool()
asyncio.run(m())
PY
# --- tear down -------------------------------------------------------------
q "DELETE FROM listings WHERE seller_id=$TSID" >/dev/null
q "DELETE FROM sellers WHERE id=$TSID" >/dev/null
rm -rf ${EVIDENCE_DIR:-./evidence}/testfixture
rm -f "$C"
echo
echo "  fixtures removed"
echo "TALLY pass=$pass fail=$fail"
