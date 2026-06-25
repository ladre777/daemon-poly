#!/bin/bash
echo "=== Build clean Python-only 'deploy' branch for Railway ==="
echo ""
TOKEN=$(printf '%s' "$GH_TOKEN" | tr -d '[:space:]')
if [ -z "$TOKEN" ]; then
  echo "!! GH_TOKEN secret is not set."
  exit 1
fi
URL="https://ladre777:${TOKEN}@github.com/ladre777/daemon-poly.git"
export GIT_ASKPASS=
export GIT_TERMINAL_PROMPT=0

echo "Step 1: saving + pushing any latest changes on main..."
git add -A
git commit -m "sync before deploy branch" || echo "  (nothing new to commit)"
git push "$URL" main || echo "  (main already up to date)"

echo ""
echo "Step 2: building the clean deploy branch in a temp folder..."
rm -rf /tmp/dp-deploy
git clone -q "$URL" /tmp/dp-deploy || { echo "!! clone failed"; exit 1; }
cd /tmp/dp-deploy || exit 1
git checkout -B deploy

echo "  Removing all JavaScript / monorepo / scaffolding files..."
rm -rf artifacts node_modules .cache
rm -f package.json pnpm-workspace.yaml pnpm-lock.yaml tsconfig.json tsconfig.base.json railpack.json
rm -f ./*.config.ts ./*.config.js 2>/dev/null

git add -A
git commit -m "Clean Python-only deploy branch for Railway" || echo "  (no changes)"

echo ""
echo "Step 3: pushing the deploy branch to GitHub..."
git push -f "$URL" deploy && PUSHED=1

cd /home/runner/workspace
echo ""
if [ "$PUSHED" = "1" ]; then
  echo "=== SUCCESS ==="
  echo "A clean 'deploy' branch is now on GitHub (Python only)."
  echo "Files on it:"
  cd /tmp/dp-deploy && git ls-files | grep -vE '^wc/|^attached_assets/' | head -40
  echo "..."
  echo "wc/ folder and bot.py are included."
else
  echo "=== PUSH FAILED - send this screen to the agent. ==="
fi
