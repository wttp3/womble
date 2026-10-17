import os, select, time, json, sys
PORT='/dev/cu.BlueToothPrinter'
def log(*a): print(time.strftime('%H:%M:%S'),*a, flush=True)
def build(mode):
    if mode=='plain':
        return b'\x1b@port check '+time.strftime('%H:%M:%S').encode()+b'\n\n\n'
    hdr=open('data/print_header.escpos','rb').read()
    wb=hdr[4]+hdr[5]*256; h=hdr[6]+hdr[7]*256; raw=hdr[8:]
    out=b'\x1b@\x1ba\x01'
    for y in range(0,h,24):
        n=min(24,h-y); out+=b'\x1d\x76\x30\x00'+bytes([wb%256,wb//256,n%256,n//256])+raw[y*wb:(y+n)*wb]
    items=[i['item'] for i in json.load(open('data/lists.json')).get('shopping',[])]
    out+=b'\n\x1b!\x30WOMBLE\n\x1b!\x00shopping list\n'+time.strftime('%a %d %b %H:%M').encode()+b'\n\x1ba\x00--------------------------------\n'
    out+=b''.join(b'[ ] '+i.encode('ascii','replace')+b'\n' for i in items)+b'\n\n\n\n'
    return out
data=build(sys.argv[1] if len(sys.argv)>1 else 'plain')
fd=os.open(PORT,os.O_RDWR|os.O_NOCTTY|os.O_NONBLOCK); log('open',len(data)); time.sleep(4)
i=0
while i<len(data):
    _,w,_=select.select([],[fd],[],5)
    if not w: log('stalled at',i); break
    try: i+=os.write(fd,data[i:i+64])
    except BlockingIOError: time.sleep(0.05); continue
    time.sleep(0.03)
log('sent',i); time.sleep(4); os.close(fd); log('closed')
