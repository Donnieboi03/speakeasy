#!/usr/bin/env bash
# Mac-only live tracker test. NEVER prompts: preflights Screen Recording + Accessibility first and exits 77 (skip) if not granted.
# Builds the tracker from the repo into a temp dir; never touches ~/.speakeasy. Usage: test_tracker_mac.sh [REPO] [FIXTURE_DIR]
set -u; REPO=${1:-$(cd "$(dirname "$0")/../../.." && pwd)}; FIX=${2:-$REPO/tests/images}; T=$(mktemp -d /tmp/se_trk_live.XXXX); H=$T/home; mkdir -p $H/logs
cat > $T/pf.swift <<'S'
import CoreGraphics; import ApplicationServices
print("{\"screen_recording\": \(CGPreflightScreenCaptureAccess()), \"accessibility\": \(AXIsProcessTrusted())}")
S
swiftc -O $T/pf.swift -o $T/pf >/dev/null 2>&1 || { echo '{"status":"error","reason":"preflight build failed"}'; exit 1; }
PF=$($T/pf); echo "preflight: $PF"
echo "$PF" | grep -q '"screen_recording": true' || { echo '{"status":"skipped","reason":"Screen Recording not granted to this process; not requesting"}'; exit 77; }
swiftc -O -parse-as-library "$REPO/src/tracker.swift" -o $T/speakeasy-track || exit 1
cat > $T/wid.swift <<'S'
import CoreGraphics; import Foundation
let owner = CommandLine.arguments[1]
let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as! [[String: Any]]
for w in list where (w[kCGWindowOwnerName as String] as? String) == owner && (w[kCGWindowLayer as String] as? Int) == 0 {
  print("\(w[kCGWindowNumber as String]!) \(w[kCGWindowOwnerPID as String]!)"); break }
S
swiftc -O $T/wid.swift -o $T/wid >/dev/null 2>&1
RES=$T/results.jsonl
for img in "$FIX"/*.png; do
  open -a Preview "$img"; sleep 2; read WID WPID <<<"$($T/wid Preview)"; [ -z "${WID:-}" ] && { echo "{\"img\":\"$img\",\"status\":\"no window\"}" >> $RES; continue; }
  sleep 30 & PARENT=$!
  /usr/bin/time -l $T/speakeasy-track --window-id $WID --pid $PARENT --home $H --fps 3 2> $T/time_$(basename $img).txt & TP=$!
  sleep 6; ps -o %cpu=,rss= -p $TP >> $T/cpu_$(basename $img).txt
  kill $PARENT; sleep 2; ALIVE=0; kill -0 $TP 2>/dev/null && ALIVE=1 && kill $TP
  echo "{\"img\":\"$(basename $img)\",\"wid\":$WID,\"exit_on_parent_death\":$([ $ALIVE = 0 ] && echo true || echo false),\"cpu_rss\":\"$(tr '\n' ' ' < $T/cpu_$(basename $img).txt)\"}" >> $RES
  osascript -e 'tell application "Preview" to close every window' >/dev/null 2>&1
done
python3 "$(dirname "$0")/audit_tracker.py" $H > $T/audit.json; cat $RES; echo "audit: $T/audit.json"
