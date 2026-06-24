#!/bin/bash
echo "=== DAEMON-POLY -> GitHub push ==="
echo ""
read -r -p "Paste your GitHub token, then press return: " RAW
TOKEN=$(printf '%s' "$RAW" | tr -d '[:space:]')
echo ""
echo "Token received: ${#TOKEN} characters."
if [ "${#TOKEN}" -lt 30 ]; then
  echo "!! Too short - the paste got cut off. Copy the token again and re-run: bash push.sh"
  exit 1
fi
echo ""
echo "Step 1: Checking the token with GitHub..."
RESP=$(curl -s -i -H "Authorization: token ${TOKEN}" https://api.github.com/user)
STATUS=$(printf '%s' "$RESP" | head -1 | tr -d '\r')
LOGIN=$(printf '%s' "$RESP" | grep -i '"login"' | head -1 | sed -E 's/.*"login": *"([^"]+)".*/\1/')
SCOPES=$(printf '%s' "$RESP" | grep -i '^x-oauth-scopes:' | sed -E 's/^[^:]*: *//I' | tr -d '\r')
echo "  GitHub replied: $STATUS"

if ! printf '%s' "$STATUS" | grep -q ' 200'; then
  echo "  !! Token was REJECTED by GitHub (not a valid token)."
  echo "  !! Generate a brand-new classic token and run: bash push.sh"
  exit 1
fi

echo "  Token is VALID. It belongs to GitHub account: $LOGIN"
echo "  Token permissions (scopes): $SCOPES"

if ! printf '%s' "$SCOPES" | grep -q 'repo'; then
  echo "  !! This token is MISSING the 'repo' scope - it cannot push code."
  echo "  !! Regenerate the token, TICK the 'repo' checkbox, then re-run: bash push.sh"
  exit 1
fi

if [ "$LOGIN" != "ladre777" ]; then
  echo ""
  echo "  !! IMPORTANT: this token is for account '$LOGIN', but the repo is owned by 'ladre777'."
  echo "  !! Either: (a) log into GitHub as 'ladre777' and make a token there, OR"
  echo "  !!         (b) tell the agent to create the repo under '$LOGIN' instead."
  echo "  !! Stopping here so we do not push to the wrong place."
  exit 1
fi

echo ""
echo "Step 2: Pushing to GitHub as $LOGIN ..."
GIT_ASKPASS= GIT_TERMINAL_PROMPT=0 git push "https://${LOGIN}:${TOKEN}@github.com/ladre777/daemon-poly.git" main
echo ""
echo "=== If you see 'main -> main' just above, IT WORKED. ==="
