#!/bin/bash
# Refresh the WorkReporter.app launcher bundle.
#
# The bundle is only a thin launcher: Contents/MacOS/WorkReporter cd's to the
# project directory (the .app's parent) and runs the top-level work_reporter.py.
# The Python source and config template are NOT duplicated inside the bundle —
# there is a single source of truth in the repo root.
#
# Run this after editing the launcher or the app icon/Info.plist. It makes the
# launcher executable and re-signs the bundle ad-hoc so macOS will launch it.
set -e

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_ROOT"
APP="WorkReporter.app"

if [ ! -d "$APP" ]; then
    echo "error: $APP not found in $REPO_ROOT" >&2
    exit 1
fi

chmod +x "$APP/Contents/MacOS/WorkReporter"

# Ad-hoc re-sign (the bundle was never Developer-ID/notarized).
codesign --force --deep -s - "$APP"

echo "Refreshed and ad-hoc signed $APP"
echo "Launch it:  open $APP   (must sit next to work_reporter.py)"
