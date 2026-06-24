#!/bin/bash
echo "=== DAEMON-POLY -> GitHub push ==="
echo ""
read -r -p "Paste your GitHub token here, then press return: " RAW
TOKEN=$(printf '%s' "$RAW" | tr -d '[:space:]')
echo ""
echo "Token received: ${#TOKEN} characters."
echo "(A correct classic token is about 40 characters and starts with ghp_)"
echo ""
if [ "${#TOKEN}" -lt 30 ]; then
  echo "!! That token looks too short - the paste was probably cut off."
  echo "!! Copy the token again on GitHub and re-run: bash push.sh"
  exit 1
fi
echo "Pushing to GitHub..."
git remote set-url github "https://ladre777:${TOKEN}@github.com/ladre777/daemon-poly.git"
git push github main
echo ""
echo "=== If you see 'main -> main' just above, IT WORKED. ==="
