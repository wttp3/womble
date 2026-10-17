import os, time, select
fd=os.open('/dev/cu.BlueToothPrinter',os.O_RDWR|os.O_NOCTTY); print('open',flush=True)
time.sleep(1)
for n in (1,2,3,4):
    os.write(fd,bytes([0x10,0x04,n]))
    r,_,_=select.select([fd],[],[],2)
    print('status',n, os.read(fd,16).hex() if r else 'no reply',flush=True)
os.write(fd,b'\x1d\x49\x01'); r,_,_=select.select([fd],[],[],2); print('model', os.read(fd,32) if r else 'no reply',flush=True)
os.close(fd)
