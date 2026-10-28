#!/usr/bin/env bash
# Secret scan for qq-bot-persist. Used as git pre-commit hook and for manual audits.
#   tools/secret-scan.sh            -> scan staged content (pre-commit)
#   tools/secret-scan.sh --tracked  -> scan all tracked files (HEAD + index)
# Blocks: forbidden paths, literal values read from local secret files (never printed),
# 32-hex openids, 9-11 digit numbers (QQ numbers / AppID), Bearer/QQBot tokens,
# password/secret assignments, emails, webhook / non-allowlisted URLs.
set -uo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT" || exit 2
MODE="${1:-staged}"
if [ "$MODE" = "--tracked" ]; then
  mapfile -t FILES < <(git ls-files)
  show() { cat -- "$1"; }
else
  mapfile -t FILES < <(git diff --cached --name-only --diff-filter=ACMR)
  show() { git show ":$1"; }
fi
[ "${#FILES[@]}" -eq 0 ] && exit 0
fail=0
report() { echo "secret-scan: $1"; fail=1; }

# 1) forbidden paths
for f in "${FILES[@]}"; do
  case "$f" in
    */appid|*/appsecret|*forwarder-url|*forwarder-key|*/url|*/key|github/*|*password*|*.bak*|*.log|*.jsonl|*.lock|*ref-cache*|*last-forward*|*local-watchdog-state*|*/c2c/*|*/_system/*|*/notes.md|*/knowledge/*|*/media/*|*bot_openid*|*group_openid.txt|*bot_uin*|*box-public-ip*|*onebot11_*|*Napcat*|*.tar.gz|*.env|*.sqlite3*|*.db|*.db-*|*dashboard-session*|*supervisor-*.json|*/groups/registry.json)
      report "forbidden path staged: $f" ;;
    official-bot/groups/*/config.json)
      [ "$f" = "official-bot/groups/_default/config.json" ] || report "real group config staged: $f" ;;
  esac
done

# 2) literal values from local secret / identity files (values are never printed)
LIT=()
for s in official-bot/appid official-bot/appsecret official-bot/bot_uin.txt official-bot/bot_openid.txt \
         official-bot/group_openid.txt forwarder-url forwarder-key github/password /root/forwarder/url /root/forwarder/key; do
  [ -r "$s" ] && v="$(tr -d '\r\n' < "$s")" && [ "${#v}" -ge 6 ] && LIT+=("$v")
done
for c in official-bot/groups/*/config.json; do
  [ -r "$c" ] || continue
  while read -r v; do [ "${#v}" -ge 6 ] && LIT+=("$v"); done < <(grep -oE '[0-9A-F]{32}|\b[0-9]{6,12}\b' "$c" | sort -u)
done

URL_ALLOW='^[0-9]+:https?://(\.\.\.$|github\.com/$|q\.qq\.com|bot\.q\.qq\.com|api\.sgroup\.qq\.com|api\.bot\.qq\.com|bots\.qq\.com|qq-ai\.cdn-go\.cn|github\.com/(x/y|<owner>|zard110/maxshenbi-qqbot)|example\.com|www\.qq\.com|qun\.qq\.com|127\.0\.0\.1(:[0-9]*)?(/|$)|localhost(:[0-9]*)?(/|$))'
for f in "${FILES[@]}"; do
  content="$(show "$f" 2>/dev/null)" || continue
  for v in "${LIT[@]}"; do
    if grep -qF -- "$v" <<<"$content"; then report "$f contains a value from a local secret/identity file (value hidden)"; fi
  done
  hits="$(grep -nE '\b[0-9A-Fa-f]{32}\b' <<<"$content")" && report "$f: 32-hex id (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
  hits="$(grep -nE '(^|[^0-9.])[0-9]{9,11}([^0-9]|$)' <<<"$content" | grep -vE '117440512')" && report "$f: 9-11 digit number (QQ/AppID?) (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
  hits="$(grep -nE '(Bearer|QQBot) [A-Za-z0-9._-]{16,}' <<<"$content")" && report "$f: literal auth token (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
  hits="$(grep -niE '(secret|password|passwd|api[_-]?key|access_token)["'"'"']?\s*[:=]\s*["'"'"'][A-Za-z0-9+/_-]{8,}' <<<"$content")" && report "$f: credential assignment (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
  hits="$(grep -noE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' <<<"$content")" && report "$f: email address (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
  hits="$(grep -noE 'https?://[^ )"'"'"'`<>]+' <<<"$content" | grep -vE "$URL_ALLOW")" && report "$f: non-allowlisted URL (webhook?) (matched lines: $(cut -d: -f1 <<<"$hits" | sort -u | head -3 | tr '\n' ','); values hidden)"
done
if [ "$fail" -ne 0 ]; then
  echo "secret-scan: BLOCKED. Remove the matching data (values are redacted) (or extend the allowlist in tools/secret-scan.sh after review)."
  exit 1
fi
echo "secret-scan: OK (${#FILES[@]} files)"
