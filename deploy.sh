#!/bin/bash
echo "=== DAEMON-POLY -> Railway fix + push ==="
echo ""
TOKEN=$(printf '%s' "$GH_TOKEN" | tr -d '[:space:]')
if [ -z "$TOKEN" ]; then
  echo "!! GH_TOKEN secret is not set. Add it in the Secrets panel and re-run."
  exit 1
fi
URL="https://ladre777:${TOKEN}@github.com/ladre777/daemon-poly.git"
export GIT_ASKPASS=
export GIT_TERMINAL_PROMPT=0

echo "Step 1: Saving local changes (Dockerfile, ignore files)..."
git add -A
git commit -m "Railway: force Python-only Docker build" || echo "  (nothing new to commit)"

echo ""
echo "Step 2: Pulling GitHub's latest and merging..."
git fetch "$URL" main || { echo "!! Fetch failed - check the token."; exit 1; }
git merge FETCH_HEAD --no-edit -m "Merge remote changes" || {
  echo "!! Merge conflict. Send this whole screen to the agent."
  exit 1
}

echo ""
echo "Step 3: Removing railpack.json (it forces the wrong build type)..."
if [ -f railpack.json ]; then
  rm -f railpack.json
  git add -A
  git commit -m "Remove railpack.json so Railway uses the Dockerfile"
  echo "  Removed."
else
  echo "  Not present, good."
fi

echo ""
echo "Step 4: Pushing to GitHub..."
git push "$URL" main && echo "" && echo "=== SUCCESS - Railway will now rebuild with the Dockerfile. ===" || echo "=== PUSH FAILED - send this screen to the agent. ==="
