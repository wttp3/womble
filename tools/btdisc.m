#import <Foundation/Foundation.h>
#import <IOBluetooth/IOBluetooth.h>
int main(int c,char**v){ @autoreleasepool{
 IOBluetoothDevice*d=[IOBluetoothDevice deviceWithAddressString:[NSString stringWithUTF8String:v[1]]];
 printf("connected=%d\n",[d isConnected]);
 if([d isConnected]){ IOReturn r=[d closeConnection]; printf("close=%x\n",r); [NSThread sleepForTimeInterval:2]; printf("connected=%d\n",[d isConnected]);}
 } return 0;}
