import os, time, select
def st(fd,n=1):
    os.write(fd,bytes([0x10,0x04,n])); r,_,_=select.select([fd],[],[],2); return os.read(fd,16).hex() if r else 'none'
fd=os.open('/dev/cu.BlueToothPrinter',os.O_RDWR|os.O_NOCTTY); time.sleep(1.5)
print('before',st(fd),flush=True)
os.write(fd,b'\x1b@'); time.sleep(0.2)
os.write(fd,b'HELLO FROM PETIT '+time.strftime('%H:%M:%S').encode()+b'\r\n'); time.sleep(0.5)
os.write(fd,b'\x1bd\x04'); time.sleep(0.5)   # feed 4 lines
os.write(fd,b'line two\r\n\x1bd\x05')
time.sleep(5); print('after',st(fd),st(fd,2),st(fd,4),flush=True)
os.close(fd)
