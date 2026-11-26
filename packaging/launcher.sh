#!/bin/sh
APP=/usr/share/APPLaunch/apps/pfadfinder
export PYTHONPATH="$APP:$APP/vendor"
exec python3 -m pfadfinder.ui "$@"
