import os, time, select
def st(fd,n=1):
    os.write(fd,bytes([0x10,0x04,n])); r,_,_=select.select([fd],[],[],2); return os.read(fd,16).hex() if r else 'none'
fd=os.open('/dev/cu.BlueToothPrinter',os.O_RDWR|os.O_NOCTTY); time.sleep(1)
print('s1',st(fd),flush=True)
os.write(fd,b'hi '+time.strftime('%H:%M:%S').encode()+b'\n\n\n\n'); time.sleep(3)
print('s2',st(fd),flush=True)
os.close(fd)
