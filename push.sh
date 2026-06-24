#!/bin/bash
echo "=== DÆMON-POLY → GitHub push ==="
echo ""
read -r -p "Paste your GitHub token here, then press return: " TOKEN
echo ""
echo "Pushing to GitHub..."
git remote set-url github "https://ladre777:${TOKEN}@github.com/ladre777/daemon-poly.git"
git push github main
echo ""
echo "=== Done. If you see no errors above, your code is on GitHub. ==="
