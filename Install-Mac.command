#!/usr/bin/env bash
# macOS: double-click to install (or update) the Continuous Claude v4.7 harness for
# Claude Code from this unzipped folder. Newer macOS blocks double-clicking a downloaded
# .command file; INSTALL-ON-MAC.txt has the Terminal line that always works.
cd "$(dirname "$0")" || exit 1
xattr -dr com.apple.quarantine . 2>/dev/null
echo "Installing the Continuous Claude v4.7 harness for Claude Code..."
bash install/quick_install.sh --yes
code=$?
echo
read -r -p "Press Return to close." _
exit $code
