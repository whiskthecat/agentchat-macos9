/* Recording Toolbox calls around the complete production sender. No OS, VM,
 * endpoint, application launch, bridge, or inference is involved. Host long
 * width is not a Classic ABI proof; actual SDK translation units provide that.
 */
#include "fake_toolbox.h"
#include "../../RemoteApplications.h"
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

static struct {
    unsigned sends, processReads, descLive, descCreated, descDisposed, uppLive;
    unsigned idleCalls, reconnects, dispatches;
    AESendMode mode;
    ProcessSerialNumber address;
    char command[1025];
    long commandBytes;
    OSErr sendError, postIdentityError;
    Boolean staleSession, changedIdentity, expire, cancel, nested, noUPP;
    unsigned long now;
    GrafPtr port;
    const char *text;
    DescType replyType;
    long appError;
    Boolean hasAppError;
} f;
static const ProcessSerialNumber target = {0,42};
static const char mutating[] = "{\"op\":\"step\"}";
static unsigned checks;

static void reset(void)
{
    assert(!RemoteApplicationsBusy());
    memset(&f,0,sizeof f);
    f.now=100;f.port=(void *)1;f.text="{\"ok\":true}";f.replyType=typeChar;
}
static Boolean idle(const EventRecord *event,void *context)
{
    RemoteApplicationReply nested;
    assert(event->what==nullEvent);assert(context==(void *)7);
    assert(RemoteApplicationsBusy());++f.idleCalls;
    f.port=(void *)2;
    if(f.nested) {
        assert(RemoteApplicationsCommand(&target,mutating,sizeof mutating-1,idle,
            (void *)7,&nested)==errAEEventFailed);
        assert(strstr(nested.detail,"Another application command"));
    }
    return f.cancel;
}
static OSErr command(const char *text,RemoteApplicationReply *result)
{
    OSErr error=RemoteApplicationsCommand(&target,text,(long)strlen(text),idle,(void *)7,result);
    assert(!RemoteApplicationsBusy());assert(f.descLive==0);assert(f.uppLive==0);
    assert(f.descCreated==f.descDisposed);assert(f.port==(void *)1);
    return error;
}
OSErr GetCurrentProcess(ProcessSerialNumber *out) {*out=(ProcessSerialNumber){0,30};return noErr;}
OSErr SameProcess(const ProcessSerialNumber *a,const ProcessSerialNumber *b,Boolean *same)
{*same=a->highLongOfPSN==b->highLongOfPSN && a->lowLongOfPSN==b->lowLongOfPSN;return noErr;}
OSErr GetProcessInformation(const ProcessSerialNumber *psn,ProcessInfoRec *out)
{
    FSSpec *app=out->processAppSpec;
    assert(psn->lowLongOfPSN==42);assert(out->processInfoLength==sizeof *out);
    ++f.processReads;
    if(f.processReads>1 && f.postIdentityError)return f.postIdentityError;
    out->processNumber=*psn;out->processType='APPL';out->processSignature='ORC2';
    out->processMode=modeHighLevelEventAware;
    out->processLaunchDate=123+(f.changedIdentity && f.processReads>1);
    app->vRefNum=1;app->parID=2;app->name[0]=4;memcpy(app->name+1,"Test",4);
    return noErr;
}
unsigned long TickCount(void) {return f.now;}
void GetPort(GrafPtr *out) {*out=f.port;}
void SetPort(GrafPtr port) {f.port=port;}
static void desc(AEDesc *out,DescType type)
{out->descriptorType=type;out->dataHandle=out;++f.descLive;++f.descCreated;}
OSErr AECreateDesc(DescType type,const void *data,Size bytes,AEAddressDesc *out)
{
    assert(type==typeProcessSerialNumber);assert(bytes==sizeof f.address);
    memcpy(&f.address,data,(size_t)bytes);desc(out,type);return noErr;
}
OSErr AECreateAppleEvent(OSType klass,OSType ident,const AEAddressDesc *address,
        short returnID,long transaction,AppleEvent *out)
{
    assert(klass=='AGCT' && ident=='cmnd');assert(address->descriptorType==typeProcessSerialNumber);
    assert(returnID==kAutoGenerateReturnID && transaction==kAnyTransactionID);
    desc(out,'aevt');return noErr;
}
OSErr AEPutParamPtr(AppleEvent *event,AEKeyword key,DescType type,const void *data,Size bytes)
{
    assert(event->dataHandle);assert(key==keyDirectObject && type==typeChar);
    assert(bytes>0 && bytes<=1024);memcpy(f.command,data,(size_t)bytes);
    f.command[bytes]=0;f.commandBytes=bytes;return noErr;
}
AEIdleUPP NewAEIdleUPP(AEIdleUPP callback) {if(f.noUPP)return NULL;++f.uppLive;return callback;}
void DisposeAEIdleUPP(AEIdleUPP callback) {assert(callback && f.uppLive);--f.uppLive;}
OSErr AESend(const AppleEvent *event,AppleEvent *reply,AESendMode mode,
        AESendPriority priority,long timeout,AEIdleUPP callback,AEFilterUPP filter)
{
    EventRecord e={nullEvent};long sleep=9;RgnHandle region=(void *)3;
    assert(event->dataHandle && callback && !filter);assert(priority==kAENormalPriority);
    assert(timeout==600);++f.sends;f.mode=mode;
    assert((mode & 3)==kAEWaitReply);assert(mode & kAENeverInteract);assert(mode & kAEDontRecord);
    assert(f.address.highLongOfPSN==target.highLongOfPSN && f.address.lowLongOfPSN==target.lowLongOfPSN);
    if(f.expire)f.now+=600;
    (void)callback(&e,&sleep,&region);assert(sleep==1 && region==NULL);
    if(f.staleSession) {
        if(mode & kAEDontReconnect)return sessClosedErr;
        ++f.reconnects;
    }
    if(f.sendError)return f.sendError;
    desc(reply,'aevt');++f.dispatches;return noErr;
}
OSErr AESizeOfParam(const AppleEvent *reply,AEKeyword key,DescType *type,Size *bytes)
{
    assert(reply->dataHandle);
    if(key==keyErrorNumber) {
        if(!f.hasAppError)return errAEDescNotFound;
        *type=typeSInt32;*bytes=sizeof f.appError;return noErr;
    }
    if(key==keyErrorString)return errAEDescNotFound;
    assert(key==keyDirectObject);*type=f.replyType;*bytes=(Size)strlen(f.text);return noErr;
}
OSErr AEGetParamPtr(const AppleEvent *reply,AEKeyword key,DescType wanted,
        DescType *actual,void *out,Size capacity,Size *received)
{
    assert(reply->dataHandle);
    if(key==keyErrorNumber) {
        assert(wanted==typeSInt32 && capacity==sizeof f.appError);
        memcpy(out,&f.appError,sizeof f.appError);*actual=typeSInt32;*received=sizeof f.appError;
    } else {
        assert(key==keyDirectObject && wanted==typeChar && capacity==(Size)strlen(f.text));
        memcpy(out,f.text,(size_t)capacity);*actual=f.replyType;*received=capacity;
    }
    return noErr;
}
OSErr AEDisposeDesc(AEDesc *value)
{
    if(value->dataHandle) {assert(f.descLive);--f.descLive;++f.descDisposed;}
    value->dataHandle=NULL;value->descriptorType=typeNull;return noErr;
}

int main(void)
{
    RemoteApplicationReply result;
    static char large[4098];
    const OSErr failures[]={sessClosedErr,errAETimeout,errAEWaitCanceled,errAEEventNotHandled,destPortErr};
    unsigned i;
    reset();f.staleSession=TRUE;
    assert(command(mutating,&result)==noErr);assert(f.sends==1 && f.reconnects==1 && f.dispatches==1);
    assert(!(f.mode & kAEDontReconnect));assert(f.processReads==2);
    assert(strcmp(f.command,mutating)==0 && result.textBytes==11);++checks;

    for(i=0;i<sizeof failures/sizeof failures[0];++i) {
        reset();f.sendError=failures[i];
        assert(command(mutating,&result)==failures[i]);assert(f.sends==1 && !f.dispatches);
        assert(!result.textBytes && !result.text[0]);assert(f.processReads==1);
        if(failures[i]==sessClosedErr) {
            assert(strstr(result.detail,"session closed"));assert(strstr(result.detail,"may have executed"));
            assert(!strstr(result.detail,"quit") && !strstr(result.detail,"restarted"));
            f.sendError=noErr;
            assert(command("{\"op\":\"status\"}",&result)==noErr);assert(f.sends==2);
            assert(strcmp(f.command,"{\"op\":\"status\"}")==0);
        }
        ++checks;
    }

    reset();f.expire=TRUE;
    assert(command(mutating,&result)==errAETimeout);assert(f.sends==1 && f.dispatches==1);
    assert(strstr(result.detail,"may have executed"));++checks;
    reset();f.now=ULONG_MAX-300;f.expire=TRUE;
    assert(command(mutating,&result)==errAETimeout);assert(f.sends==1);++checks;
    reset();f.cancel=TRUE;f.sendError=errAEWaitCanceled;
    assert(command(mutating,&result)==errAEWaitCanceled);assert(f.sends==1 && f.idleCalls==1);++checks;
    reset();f.nested=TRUE;
    assert(command(mutating,&result)==noErr);assert(f.sends==1 && f.idleCalls==1);++checks;

    reset();f.changedIdentity=TRUE;
    assert(command(mutating,&result)==procNotFound);assert(f.sends==1 && f.processReads==2);
    assert(strstr(result.detail,"changed identity"));++checks;
    reset();f.postIdentityError=procNotFound;
    assert(command(mutating,&result)==procNotFound);assert(f.sends==1);++checks;
    reset();f.noUPP=TRUE;
    assert(command(mutating,&result)==memFullErr);assert(f.sends==0);++checks;
    reset();f.hasAppError=TRUE;f.appError=70001;
    assert(command(mutating,&result)==errAEEventFailed);assert(result.applicationError==70001 && f.sends==1);++checks;
    reset();memset(large,'x',4096);large[4096]=0;f.text=large;
    assert(command(mutating,&result)==noErr && result.textBytes==4096);++checks;
    reset();large[4096]='x';large[4097]=0;f.text=large;
    assert(command(mutating,&result)==errAEBufferTooSmall && f.sends==1);++checks;
    reset();f.replyType=typeSInt32;
    assert(command(mutating,&result)==errAEWrongDataType && f.sends==1);++checks;
    printf("%u native sender recording scenarios passed; no Toolbox or inference used\n",checks);
    return 0;
}
