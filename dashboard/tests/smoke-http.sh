#!/usr/bin/env bash
# Spark Cockpit HTTP smoke: every security path and endpoint, live.
# Usage: bash dashboard/tests/smoke-http.sh   (cockpit must be running)
set -u
BASE="${COCKPIT_BASE:-http://127.0.0.1:30090}"
KEY="$(cat "${COCKPIT_KEY_FILE:-$HOME/.config/qwen38/api-key}")"
J="$(mktemp)"; trap 'rm -f "$J"' EXIT
pass=0; fail=0
ck(){ # ck <name> <expected> <got>
  if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "  ok   $1"
  else fail=$((fail+1)); echo "  FAIL $1 (attendu $2, recu $3)"; fi
}
code(){ curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "── sans auth:"
ck "health public"        200 "$(code "$BASE/api/health")"
ck "state sans session"   401 "$(code "$BASE/api/state")"
ck "action sans session"  401 "$(code -X POST "$BASE/api/action" -H 'Content-Type: application/json' -d '{}')"
ck "login mauvaise cle"   403 "$(code -X POST "$BASE/api/login" -H 'Content-Type: application/json' -d '{"key":"wrong"}')"

echo "── session:"
ck "login bonne cle"      200 "$(code -c "$J" -X POST "$BASE/api/login" -H 'Content-Type: application/json' -d "{\"key\":\"$KEY\"}")"
ck "state avec session"   200 "$(code -b "$J" "$BASE/api/state")"
ck "registry"             200 "$(code -b "$J" "$BASE/api/registry")"
ck "upstream (cache)"     200 "$(code -b "$J" "$BASE/api/upstream")"
ck "recipes"              200 "$(code -b "$J" "$BASE/api/recipes")"
ck "recipes: 5 builtin, flash sans derive" "5 0" "$(curl -s -b "$J" "$BASE/api/recipes" | python3 -c 'import json,sys; d=json.load(sys.stdin); f=[b for b in d["builtin"] if b["recipe"]["id"]=="flash"][0]; print(len(d["builtin"]), len(f["drift"] or []))')"
ck "inventory"            200 "$(code -b "$J" "$BASE/api/inventory")"
ck "logs allowlist"       404 "$(code -b "$J" "$BASE/api/logs/etc-passwd")"
ck "static traversal"     404 "$(code -b "$J" "$BASE/static/../cockpit.py")"

echo "── actions:"
ck "action sans csrf"     403 "$(code -b "$J" -X POST "$BASE/api/action" -H 'Content-Type: application/json' -d '{"name":"smoke","params":{}}')"
CSRF=$(curl -s -b "$J" -X POST "$BASE/api/csrf" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
ck "enum ferme"           400 "$(code -b "$J" -X POST "$BASE/api/action" -H 'Content-Type: application/json' -d "{\"name\":\"unit\",\"params\":{\"verb\":\"start\",\"unit\":\"evil.service\"},\"csrf\":\"$CSRF\"}")"
ck "action inconnue"      404 "$(code -b "$J" -X POST "$BASE/api/action" -H 'Content-Type: application/json' -d "{\"name\":\"rm-rf\",\"params\":{},\"csrf\":\"$CSRF\"}")"
# the mutual-exclusion gate: starting the OTHER engine while one is busy
BUSY=$(curl -s -b "$J" "$BASE/api/state" | python3 -c '
import json,sys
l=json.load(sys.stdin)["lifecycle"]["data"]["engines"]
busy=[u for u,e in l.items() if e["state"] not in ("stopped","failed")]
other={"qwen38-flash.service":"qwen38-sglang.service","qwen38-sglang.service":"qwen38-flash.service"}
print(other[busy[0]] if busy else "")')
if [ -n "$BUSY" ]; then
  CSRF=$(curl -s -b "$J" -X POST "$BASE/api/csrf" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
  ck "gate deux moteurs"  409 "$(code -b "$J" -X POST "$BASE/api/action" -H 'Content-Type: application/json' -d "{\"name\":\"unit\",\"params\":{\"verb\":\"start\",\"unit\":\"$BUSY\"},\"csrf\":\"$CSRF\"}")"
else
  echo "  skip gate deux moteurs (aucun moteur actif)"
fi

echo "── agent (relais opencode):"
AG=$(curl -s -b "$J" "$BASE/api/state" | python3 -c 'import json,sys; a=(json.load(sys.stdin).get("agent") or {}).get("data") or {}; r=a.get("relay") or {}; print("1" if a.get("enabled") else "0", r.get("bind") or "-", r.get("port") or "-", "1" if r.get("listening") else "0")')
read -r AG_ON AG_BIND AG_PORT AG_LISTEN <<<"$AG"
if [ "$AG_ON" = "1" ] && [ "$AG_LISTEN" = "1" ]; then
  # the session cookie is per host: log in again through the relay's own address
  CB="http://$AG_BIND:${BASE##*:}"; R="http://$AG_BIND:$AG_PORT"; J2="$(mktemp)"
  if [ "$(code -c "$J2" -X POST "$CB/api/login" -H 'Content-Type: application/json' -d "{\"key\":\"$KEY\"}")" = "200" ]; then
    ck "relais sans session"        401 "$(code "$R/global/health")"
    ck "relais page de connexion"   401 "$(code -H 'Accept: text/html' "$R/")"
    ck "relais origine etrangere"   403 "$(code -b "$J2" -H 'Origin: http://evil.example' "$R/global/health")"
    ck "relais avec session"        200 "$(code -b "$J2" "$R/global/health")"
    ck "relais sante opencode"      true "$(curl -s -b "$J2" "$R/global/health" | python3 -c 'import json,sys; print(str(json.load(sys.stdin).get("healthy")).lower())')"
    ck "relais interface"           200 "$(code -b "$J2" -H 'Accept: text/html' "$R/")"
    ck "relais frame-ancestors"     1 "$(curl -s -D - -o /dev/null -b "$J2" "$R/global/health" | grep -ci 'frame-ancestors')"
    ck "relais nosniff"             1 "$(curl -s -D - -o /dev/null -b "$J2" "$R/global/health" | grep -ci 'x-content-type-options: nosniff')"
    ck "cockpit CSP frame-src"      1 "$(curl -s -D - -o /dev/null -b "$J2" "$CB/" | grep -ci 'frame-src http://')"
    ck "journal opencode-web"       200 "$(code -b "$J2" "$CB/api/logs/opencode-web.service")"
    ck "relais websocket sans session" 401 "$(code -H 'Upgrade: websocket' -H 'Connection: Upgrade' -H 'Sec-WebSocket-Key: dGVzdA==' -H 'Sec-WebSocket-Version: 13' "$R/pty/x/connect")"
    # the permission mode the unit declares is the one the running server applies
    ck "mode permissions unite = serveur" "same" "$(curl -s -b "$J2" "$CB/api/state" | python3 -c 'import json,sys; a=json.load(sys.stdin)["agent"]["data"]; print("same" if a.get("auto") is not None and a.get("auto")==a.get("auto_live") else "%s vs %s" % (a.get("auto"), a.get("auto_live")))')"
  else
    echo "  skip agent: le cockpit ne repond pas sur $CB (le relais et le cockpit doivent partager un hote)"
  fi
  rm -f "$J2"
else
  echo "  skip agent (relais non installe ou pas encore lie)"
fi

echo "── en-tetes:"
H=$(curl -s -D - -o /dev/null -b "$J" "$BASE/api/state")
echo "$H" | grep -qi 'content-security-policy' && ck "CSP presente" 1 1 || ck "CSP presente" 1 0
echo "$H" | grep -qi 'x-content-type-options: nosniff' && ck "nosniff" 1 1 || ck "nosniff" 1 0

echo; echo "TOTAL: $pass ok, $fail echecs"
[ "$fail" -eq 0 ]
