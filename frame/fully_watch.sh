#!/bin/bash
# keeps Fully Kiosk in front on the kitchen frame (vendor blocks Fully's boot-start)
# talks to the frame over adb wifi (persist.adb.tcp.port=5555 set on the frame)
export ADB_LIBUSB=1  # native mac usb backend hangs after power cuts
A="${ADB:-$HOME/platform-tools/adb}"
# frame address lives in the untracked ../local.env (FRAME_HOST=<frame-ip>, optional FRAME_ADB_PORT)
L="$(cd "$(dirname "$0")/.." && pwd)/local.env"
[ -f "$L" ] && { set -a; . "$L"; set +a; }
if [ -z "${FRAME_HOST:-}" ]; then echo "$(date '+%F %T') FRAME_HOST not set (see local.env.example); idle"; sleep 300; exit 1; fi
F="$FRAME_HOST:${FRAME_ADB_PORT:-5555}"
while true; do
  if [ "$($A -s $F get-state 2>/dev/null)" != "device" ]; then
    $A disconnect $F >/dev/null 2>&1; $A connect $F >/dev/null 2>&1
  fi
  if [ "$($A -s $F get-state 2>/dev/null)" = "device" ]; then
    if [ "$($A -s $F shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
      f=$($A -s $F shell dumpsys window windows 2>/dev/null | grep -m1 mCurrentFocus)
      case "$f" in
        *de.ozerov.fully*|*KeyguardScrim*|"") ;;
        *nexuslauncher*) echo "$(date '+%F %T') focus was: $f -> starting Fully"; $A -s $F shell am start -n de.ozerov.fully/.MainActivity >/dev/null 2>&1 ;;
      esac
    fi
  fi
  sleep 20
done
