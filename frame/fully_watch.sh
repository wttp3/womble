#!/bin/bash
# keeps Fully Kiosk in front on the kitchen frame (vendor blocks Fully's boot-start)
# talks to the frame over adb wifi (persist.adb.tcp.port=5555 set on the frame)
export ADB_LIBUSB=1  # native mac usb backend hangs after power cuts
A=/Users/decoy/platform-tools/adb
F=192.168.0.160:5555
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
