#!/usr/bin/env bash
# Read-only RAM snapshot of the speakeasy pipeline (no UI, no permissions). macOS: physical footprint via `footprint` (includes Metal); Linux: RSS.
# Usage: ram_probe.sh [--json]   Budget: total < 8192 MB, aim ~4096 MB.
pat='ollama|llama-server|mlx_audio|speakeasy|tracker|Speakeasy'
tot=0; rows=""
for pid in $(pgrep -f -i "$pat"); do
  cmd=$(ps -o comm= -p "$pid" 2>/dev/null | sed 's#.*/##'); [ -z "$cmd" ] && continue
  mb=""
  if command -v footprint >/dev/null 2>&1; then mb=$(footprint -p "$pid" 2>/dev/null | awk '/phys_footprint:/{v=$2;u=$3; if(u~/GB/)v*=1024; if(u~/KB/)v/=1024; print int(v); exit}'); fi
  [ -z "$mb" ] && mb=$(( $(ps -o rss= -p "$pid" 2>/dev/null || echo 0) / 1024 ))
  tot=$((tot+mb)); rows="$rows{\"pid\":$pid,\"proc\":\"$cmd\",\"mb\":$mb},"
done
st=PASS; [ $tot -ge 8192 ] && st=FAIL
if [ "$1" = "--json" ]; then echo "{\"total_mb\":$tot,\"status\":\"$st\",\"aim_ok\":$([ $tot -le 4096 ] && echo true || echo false),\"procs\":[${rows%,}]}"; else echo "$rows" | tr '}' '\n'; echo "total_mb=$tot status=$st (budget <8192, aim ~4096)"; fi
