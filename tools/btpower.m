#import <Foundation/Foundation.h>
int IOBluetoothPreferenceGetControllerPowerState(void);
void IOBluetoothPreferenceSetControllerPowerState(int);
int main(int argc,char**argv){ if(argc>1) IOBluetoothPreferenceSetControllerPowerState(atoi(argv[1])); printf("%d\n",IOBluetoothPreferenceGetControllerPowerState()); return 0;}
