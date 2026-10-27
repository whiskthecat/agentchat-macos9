#ifndef REMOTE_APPLICATIONS_FAKE_TOOLBOX_H
#define REMOTE_APPLICATIONS_FAKE_TOOLBOX_H
#include <stdint.h>
#define pascal
#define TRUE 1
#define FALSE 0
typedef unsigned char Boolean;
typedef int16_t OSErr;
typedef uint32_t OSType, DescType, AEKeyword;
typedef long Size, AESendMode, AESendPriority;
typedef void *GrafPtr, *RgnHandle;
typedef struct { unsigned long highLongOfPSN, lowLongOfPSN; } ProcessSerialNumber;
typedef struct { short vRefNum; long parID; unsigned char name[256]; } FSSpec;
typedef struct {
    unsigned long processInfoLength;
    ProcessSerialNumber processNumber;
    OSType processType, processSignature;
    unsigned long processMode, processLaunchDate;
    FSSpec *processAppSpec;
} ProcessInfoRec;
typedef struct { short what; } EventRecord;
typedef struct { DescType descriptorType; void *dataHandle; } AEDesc;
typedef AEDesc AEAddressDesc, AppleEvent;
typedef Boolean (*AEIdleUPP)(EventRecord *, long *, RgnHandle *);
typedef void *AEFilterUPP;
enum {
    noErr=0, paramErr=-50, memFullErr=-108, procNotFound=-600,
    destPortErr=-906, sessClosedErr=-917, errAEDescNotFound=-1701,
    errAEWrongDataType=-1703, errAEEventNotHandled=-1708,
    errAEWaitCanceled=-1711, errAETimeout=-1712, errAEBufferTooSmall=-1741,
    errAEEventFailed=-10000, kCurrentProcess=2, modeHighLevelEventAware=0x40,
    nullEvent=0, updateEvt=6, activateEvt=8, osEvt=15,
    kAEWaitReply=3, kAENeverInteract=16, kAEDontReconnect=128,
    kAEDontRecord=4096, kAENormalPriority=0, kAutoGenerateReturnID=-1,
    kAnyTransactionID=0
};
#define typeNull ((DescType)0)
#define typeProcessSerialNumber ((DescType)'psn ')
#define typeChar ((DescType)'TEXT')
#define typeSInt32 ((DescType)'long')
#define keyDirectObject ((AEKeyword)'----')
#define keyErrorNumber ((AEKeyword)'errn')
#define keyErrorString ((AEKeyword)'errs')
OSErr GetCurrentProcess(ProcessSerialNumber *);
OSErr SameProcess(const ProcessSerialNumber *,const ProcessSerialNumber *,Boolean *);
OSErr GetProcessInformation(const ProcessSerialNumber *,ProcessInfoRec *);
unsigned long TickCount(void);
void GetPort(GrafPtr *);
void SetPort(GrafPtr);
OSErr AECreateDesc(DescType,const void *,Size,AEAddressDesc *);
OSErr AECreateAppleEvent(OSType,OSType,const AEAddressDesc *,short,long,AppleEvent *);
OSErr AEPutParamPtr(AppleEvent *,AEKeyword,DescType,const void *,Size);
AEIdleUPP NewAEIdleUPP(AEIdleUPP);
void DisposeAEIdleUPP(AEIdleUPP);
OSErr AESend(const AppleEvent *,AppleEvent *,AESendMode,AESendPriority,long,AEIdleUPP,AEFilterUPP);
OSErr AESizeOfParam(const AppleEvent *,AEKeyword,DescType *,Size *);
OSErr AEGetParamPtr(const AppleEvent *,AEKeyword,DescType,DescType *,void *,Size,Size *);
OSErr AEDisposeDesc(AEDesc *);
#endif
