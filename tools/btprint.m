#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>
@interface D:NSObject<IOBluetoothRFCOMMChannelDelegate> @property BOOL open; @property NSMutableData*rx; @end
@implementation D
-(void)rfcommChannelOpenComplete:(IOBluetoothRFCOMMChannel*)c status:(IOReturn)e{printf("openComplete %x\n",e);self.open=(e==0);}
-(void)rfcommChannelData:(IOBluetoothRFCOMMChannel*)c data:(void*)p length:(size_t)n{[self.rx appendBytes:p length:n];}
-(void)rfcommChannelClosed:(IOBluetoothRFCOMMChannel*)c{printf("closed\n");self.open=NO;}
@end
static void spin(double s){[[NSRunLoop currentRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:s]];}
int main(int c,char**v){@autoreleasepool{
 IOBluetoothDevice*d=[IOBluetoothDevice deviceWithAddressString:[NSString stringWithUTF8String:v[1]]];
 NSData*data=[NSData dataWithContentsOfFile:[NSString stringWithUTF8String:v[2]]];
 [d performSDPQuery:nil]; spin(3);
 BluetoothRFCOMMChannelID ch=0; 
 for(IOBluetoothSDPServiceRecord*r in d.services){BluetoothRFCOMMChannelID x; if([r getRFCOMMChannelID:&x]==0){printf("service %s ch %d\n",[[r getServiceName]?:@"?" UTF8String],x); if(!ch) ch=x;}}
 if(c>3) ch=atoi(v[3]);
 if(!ch){printf("no rfcomm\n");return 1;}
 D*del=[D new]; del.rx=[NSMutableData data]; IOBluetoothRFCOMMChannel*chan=nil;
 IOReturn r=[d openRFCOMMChannelAsync:&chan withChannelID:ch delegate:del]; printf("open ch %d -> %x\n",ch,r);
 for(int i=0;i<100&&!del.open;i++) spin(0.1);
 if(!del.open){printf("not open\n");return 2;}
 uint16_t mtu=[chan getMTU]; printf("mtu %d bytes %lu\n",mtu,(unsigned long)data.length);
 const uint8_t*p=data.bytes; size_t off=0;
 while(off<data.length){ size_t n=MIN((size_t)128,data.length-off); IOReturn w=[chan writeSync:(void*)(p+off) length:n]; if(w){printf("write err %x at %zu\n",w,off);break;} off+=n; spin(0.04);}
 printf("sent %zu\n",off);
 uint8_t q[3]={0x10,0x04,0x01}; [chan writeSync:q length:3]; spin(2);
 printf("rx %s\n",[[del.rx description] UTF8String]);
 [chan closeChannel]; spin(1); [d closeConnection];
 }return 0;}
