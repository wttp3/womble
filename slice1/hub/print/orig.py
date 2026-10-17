import os, time
fd = os.open('/dev/cu.BlueToothPrinter', os.O_RDWR | os.O_NOCTTY)
print('open',flush=True)
data = b'\x1b@' + b'\x1ba\x01' + b'WOMBLE #1\n' + b'\x1ba\x00' + b'printer test from petit\n' + time.strftime('%H:%M %d %b').encode() + b'\n\n\n\n'
print(os.write(fd, data),flush=True); time.sleep(2); os.close(fd); print('ok')
