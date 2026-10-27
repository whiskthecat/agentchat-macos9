#!/usr/bin/env python3
"""Execute native discovery and connection/prefs functions with recording OT fakes.

No socket, prompt, inference, physical Mac or VM is used by these host checks.
"""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


def function(source, name):
    match = re.search(r'^static (?:pascal )?(?:void|Boolean|OSErr) ' + name + r'\([^;]*?\)\s*\{', source, re.M)
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end] + '\n'


PREFIX = r'''
#include <stdint.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
typedef uint32_t UInt32;
typedef uint16_t UInt16;
typedef unsigned char UInt8, Boolean;
typedef long OSStatus;
typedef short OSErr;
typedef uint32_t InetHost;
typedef uint16_t InetPort;
typedef int EndpointRef;
typedef uint32_t OTFlags;
typedef struct { UInt16 fAddressType, fPort; UInt32 fHost; } InetAddress;
typedef struct { UInt8 *buf; long len, maxlen; } TNetbuf;
typedef struct { TNetbuf addr, opt, udata; } TUnitData;
typedef struct { TNetbuf addr; int qlen; } TBind;
typedef struct { TNetbuf addr; } TCall;
typedef struct { UInt32 len, level, name, status, value[1]; } TOption;
typedef struct { TNetbuf opt; int flags; } TOptMgmt;
typedef struct { int unused; } ProcessSerialNumber;
typedef struct { int unused; } FSSpec;
#define TRUE 1
#define FALSE 0
#define noErr 0
#define kOTNoError 0
#define kOTNoDataErr -3162
#define kOTLookErr -3158
#define T_UDERR 64
#define T_CONNECT 2
#define T_DISCONNECT 16
#define T_ORDREL 128
#define AF_INET 2
#define INET_IP 0
#define IP_BROADCAST 32
#define T_YES 1
#define T_NEGOTIATE 4
#define T_SUCCESS 32
#define kUDPName "udp"
#define kTCPName "tcp"
#define AGENT_PORT 8765
#define AGENT_HOST ""
#define HTTP_PORT 8767
#define APP_CREATOR 'AGCH'
#define PREF_STARTUP 1
#define PREF_DISCOVERY_OFF 2
#define kOnSystemDisk 0
#define kPreferencesFolderType 0
#define fsRdCmd 0
#define fsRdWrCmd 1
#define fnfErr -43
#define dupFNErr -48
static UInt32 ticks=1000;
static int udpOpens, tcpOpens, probes, closes, connects, writes, failConnect;
static int optionFail, bindFail, nonblockFail, receiveReady, udpBound;
static int diskSize, readSize;
static unsigned char disk[1024];
static char packet[256], lastTarget[64];
static long packetLength;
static InetAddress packetSource;
static UInt32 packetFlags;
static OSStatus InitOpenTransport(void){return 0;}
static UInt32 TickCount(void) { return ticks; }
static const char *OTCreateConfiguration(const char *s) { return s; }
static EndpointRef OTOpenEndpoint(const char *c,int f,void *i,OSStatus *e) {
    (void)f;(void)i;*e=0;if(!strcmp(c,"udp")){udpOpens++;udpBound=0;return 10;}tcpOpens++;return 20;
}
static OSStatus OTCloseProvider(EndpointRef ep) { (void)ep;closes++;return 0; }
static OSStatus OTOptionManagement(EndpointRef ep,TOptMgmt *a,TOptMgmt *b) {
    (void)ep;(void)b;TOption *o=(TOption*)a->opt.buf;assert(udpBound && o->name==IP_BROADCAST && o->len==sizeof(TOption) && o->value[0]==T_YES);
    o->status=T_SUCCESS;return optionFail;
}
static OSStatus OTBind(EndpointRef ep,TBind *a,void *b){(void)a;(void)b;if(ep==10 && !bindFail)udpBound=1;return bindFail;}
static OSStatus OTSetNonBlocking(EndpointRef ep){(void)ep;return nonblockFail;}
static OSStatus OTSetAsynchronous(EndpointRef ep){(void)ep;return 0;}
static OSStatus GetCurrentProcess(ProcessSerialNumber *p){(void)p;return 0;}
#define NewOTNotifyUPP(x) 0
static OSStatus OTInstallNotifier(EndpointRef ep,int p,void *c){(void)ep;(void)p;(void)c;return 0;}
static void OTInitInetAddress(InetAddress *a,InetPort p,InetHost h){a->fAddressType=AF_INET;a->fPort=p;a->fHost=h;}
static OSStatus OTSndUData(EndpointRef ep,TUnitData *d){
    (void)ep;assert(d->udata.len==30);assert(((InetAddress*)d->addr.buf)->fHost==0xffffffffU);
    probes++;return 0;
}
static OSStatus OTRcvUData(EndpointRef ep,TUnitData *d,OTFlags *f){
    (void)ep;if(!receiveReady)return kOTNoDataErr;receiveReady=0;
    memcpy(d->udata.buf,packet,packetLength);d->udata.len=packetLength;
    memcpy(d->addr.buf,&packetSource,sizeof packetSource);d->addr.len=sizeof packetSource;
    *f=packetFlags;return 0;
}
static OSStatus OTLook(EndpointRef ep){(void)ep;return 0;}
static OSStatus OTRcvUDErr(EndpointRef ep,void *e){(void)ep;(void)e;return 0;}
static OSStatus OTConnect(EndpointRef ep,TCall *c,void *r){
    (void)ep;(void)r;InetAddress *a=(InetAddress*)c->addr.buf;
    sprintf(lastTarget,"%u.%u.%u.%u:%u",a->fHost>>24,(a->fHost>>16)&255,(a->fHost>>8)&255,a->fHost&255,a->fPort);
    connects++;return failConnect ? -1 : kOTNoDataErr;
}
static Boolean OTEnterNotifier(EndpointRef ep){(void)ep;return 1;}
static void OTLeaveNotifier(EndpointRef ep){(void)ep;}
static OSStatus OTRcvConnect(EndpointRef ep,void *c){(void)ep;(void)c;return failConnect;}
static OSStatus OTRcvOrderlyDisconnect(EndpointRef ep){(void)ep;return 0;}
static void OTRemoveNotifier(EndpointRef ep){(void)ep;}
static void OTSndOrderlyDisconnect(EndpointRef ep){(void)ep;}
#define OTMemzero(p,n) memset(p,0,n)
static void ToolQuestionsReset(void){}
static void ChatImagesDisconnect(void){}
static void RemoteMacReset(void){}
static char status[256];
static void set_status(const char *s){strcpy(status,s);}
static void boot_trace(const char *s){(void)s;}
static void flush_cur(void){}
static void push_line(const char *s){(void)s;}
static void nav_new_chat(void){}
static void history_line(char c,char *p){(void)c;(void)p;}
static int gChatCount,gModelCount;
static char gActiveID[96],gNewDraft[2049],gModelID[128];
static int gQLen,gQPos,gAccumLen,gRecvLen,gRecvPos;
static Boolean gHistoryLoading,gDraftCached,gBusy,gDirty,gNavDirty;
static OSErr preferences_spec(FSSpec *s){(void)s;return 0;}
static OSErr FSpOpenDF(FSSpec *s,int mode,short *f){(void)s;(void)mode;*f=1;return 0;}
static OSErr FSRead(short fd,long *n,void *p){(void)fd;if(*n>diskSize)*n=diskSize;memcpy(p,disk,*n);readSize=(int)*n;return 0;}
static OSErr FSClose(short f){(void)f;return 0;}
static OSErr FSpCreate(FSSpec *s,int c,int t,int x){(void)s;(void)c;(void)t;(void)x;return 0;}
static OSErr SetEOF(short f,long n){(void)f;diskSize=(int)n;return 0;}
static OSErr FSWrite(short f,long *n,void *p){(void)f;assert(*n<1024);memcpy(disk,p,*n);diskSize=(int)*n;writes++;return 0;}
'''

SUFFIX = r'''
static void response(const char *suffix) {
    sprintf(packet,"AGENTCHAT_RELAY 1 %s %s",nonce,suffix);packetLength=strlen(packet);
    receiveReady=1;packetSource.fAddressType=AF_INET;packetSource.fHost=0xc0a80164;
    packetSource.fPort=AGENT_PORT;packetFlags=0;
}
static void parse(void){
    DiscoveredRelay r;
    assert(RelayDiscoveryStart(AGENT_PORT));response("12345 8767\n");
    assert(parse_reply(packet,packetLength,0xc0a80164,AGENT_PORT,&r));
    assert(!strcmp(r.host,"192.168.1.100")&&r.chatPort==12345&&r.updatePort==8767);
    const char *bad[]={"0 8767\n","65536 8767\n","8765 0\n","8765 999999999999999\n","+8765 8767\n","8765 8767\nextra","8765 8767","8765 8767\r\n","8765  8767\n"};
    for(unsigned int i=0;i<sizeof(bad)/sizeof(*bad);i++) { response(bad[i]);assert(!parse_reply(packet,packetLength,0xc0a80164,AGENT_PORT,&r)); }
    response("8765 8767\n");assert(!parse_reply(packet,packetLength,0,AGENT_PORT,&r));
    assert(!parse_reply(packet,packetLength,0xe0000001,AGENT_PORT,&r));
    assert(!parse_reply(packet,packetLength,0xc0a80164,99,&r));packet[17]='z';
    assert(!parse_reply(packet,packetLength,0xc0a80164,AGENT_PORT,&r));RelayDiscoveryCancel();
}
static void bounded(void){
    DiscoveredRelay r;assert(RelayDiscoveryStart(AGENT_PORT));
    assert(!RelayDiscoveryPoll(&r)&&probes==1);ticks+=59;RelayDiscoveryPoll(&r);assert(probes==1);
    ticks++;RelayDiscoveryPoll(&r);assert(probes==2);ticks+=179;assert(!RelayDiscoveryPoll(&r));
    ticks++;assert(RelayDiscoveryPoll(&r)==-1&&!RelayDiscoveryActive()&&probes==2);
    optionFail=1;assert(!RelayDiscoveryStart(AGENT_PORT)&&!RelayDiscoveryActive());optionFail=0;
    bindFail=1;assert(!RelayDiscoveryStart(AGENT_PORT)&&!RelayDiscoveryActive());bindFail=0;
    nonblockFail=1;assert(!RelayDiscoveryStart(AGENT_PORT)&&!RelayDiscoveryActive());
}
static void preferences(void){
    ConnectionPrefs saved=gPrefs;strcpy(saved.host,"192.168.1.173");saved.startup=0;
    strcpy(saved.lastID,"kept-conversation");strcpy(saved.model,"kept-model");
    memcpy(disk,&saved,sizeof saved);diskSize=sizeof saved;load_preferences();
    assert(!memcmp(&gPrefs,&saved,sizeof saved));
    assert(!(gPrefs.startup&PREF_STARTUP)&&!(gPrefs.startup&PREF_DISCOVERY_OFF));
    gPrefs.startup=2;gPrefs.host[0]=0;save_preferences();gPrefs=saved;load_preferences();
    assert(gPrefs.startup==2&&!gPrefs.host[0]&&diskSize==sizeof(ConnectionPrefs));
    LegacyConnectionPrefs old={'AC01',"10.0.2.2",8865,8767,1,"old-chat"};
    memcpy(disk,&old,sizeof old);diskSize=sizeof old;load_preferences();
    assert(!strcmp(gPrefs.host,"10.0.2.2")&&gPrefs.chatPort==8865&&gPrefs.startup==1&&!strcmp(gPrefs.lastID,"old-chat"));
}
static void saved(void){
    strcpy(gPrefs.host,"192.168.1.173");strcpy(gPrefs.lastID,"keep");gPrefs.chatPort=4444;
    try_connect();assert(connects==1&&!udpOpens&&!strcmp(lastTarget,"192.168.1.173:4444"));
    g_otEvents=T_CONNECT;pump_ot_events();assert(gConnected&&!writes&&!udpOpens&&!strcmp(gPrefs.lastID,"keep"));
    try_connect();assert(connects==1&&!udpOpens);
}
static void fresh(void){
    assert(!gPrefs.host[0]);try_connect();assert(RelayDiscoveryActive()&&!connects);
    try_connect();assert(probes==1);response("12345 23456\n");try_connect();
    assert(connects==1&&!writes&&!gPrefs.host[0]&&!strcmp(lastTarget,"192.168.1.100:12345"));
    g_otEvents=T_CONNECT;pump_ot_events();assert(gConnected&&writes==1);
    assert(!strcmp(gPrefs.host,"192.168.1.100")&&gPrefs.chatPort==12345&&gPrefs.updatePort==23456);
    int opened=udpOpens;close_conn();ticks+=400;try_connect();assert(connects==2&&udpOpens==opened);
}
static void failure(void){
    strcpy(gPrefs.host,"192.168.1.173");try_connect();g_otEvents=T_DISCONNECT;
    pump_ot_events();assert(!gConnected&&gDiscoverNext);ticks+=301;try_connect();
    assert(RelayDiscoveryActive()&&connects==1);response("12345 8767\n");try_connect();
    assert(connects==2&&!writes);failConnect=1;g_otEvents=T_CONNECT;pump_ot_events();
    assert(!writes&&!strcmp(gPrefs.host,"192.168.1.173"));
}
static void timeout(void){
    strcpy(gPrefs.host,"192.168.1.173");try_connect();ticks+=721;try_connect();
    assert(gConnPhase==0&&gDiscoverNext);try_connect();assert(RelayDiscoveryActive());
}
static void off(void){
    gPrefs.startup=3;strcpy(gPrefs.host,"192.168.1.173");failConnect=1;
    try_connect();ticks+=400;try_connect();assert(connects==2&&!udpOpens);
    close_conn();gPrefs.host[0]=0;ticks+=2000;try_connect();assert(!udpOpens&&connects==2);
    gAutoConnect=FALSE;gPrefs.startup=1;try_connect();assert(!udpOpens);
}
int main(int argc,char **argv){
    assert(argc==2);
    if(!strcmp(argv[1],"parse"))parse();else if(!strcmp(argv[1],"bounded"))bounded();
    else if(!strcmp(argv[1],"preferences"))preferences();else if(!strcmp(argv[1],"saved"))saved();
    else if(!strcmp(argv[1],"fresh"))fresh();else if(!strcmp(argv[1],"failure"))failure();
    else if(!strcmp(argv[1],"timeout"))timeout();else if(!strcmp(argv[1],"off"))off();else assert(0);
    puts(argv[1]);return 0;
}
'''


class NativeDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        source = (ROOT / 'agentchat.c').read_text()
        header = (ROOT / 'RelayDiscovery.h').read_text()
        module = (ROOT / 'RelayDiscovery.c').read_text()
        clean = lambda text: re.sub(r'^#include [<"].*$', '', text, flags=re.M)
        prefs = source[source.index('typedef struct {\n    UInt32 magic;'):source.index('static UInt32      gLastTry')]
        # Notifier is irrelevant to host checks; production builds check its ABI.
        prefs = prefs[:prefs.index('/* OT async notifier')].replace('#ifndef mc68000','')
        state = '''static EndpointRef gEP; static Boolean gConnected, gAutoConnect=TRUE;
static Boolean gRelayHeartbeat,gRelayGreeted; static UInt32 gRelayLastReceive,gLastTry;\n'''
        funcs = source[source.index('static UInt32 ip_to_nbo'):source.index('/* FSpOpenDF')]
        funcs += ''.join(function(source, n) for n in ['valid_address','load_preferences','save_preferences','select_relay','close_conn','try_connect','pump_ot_events'])
        path = Path(cls.scratch.name) / 'native.c'
        path.write_text(PREFIX + clean(header) + clean(module) + prefs + state + funcs + SUFFIX)
        cls.binary = str(path.with_suffix(''))
        subprocess.run(['cc','-std=c99','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer',
                        '-Wno-multichar',str(path),'-o',cls.binary],check=True)

    def test_production_functions(self):
        for case in ['parse','bounded','preferences','saved','fresh','failure','timeout','off']:
            with self.subTest(case=case):
                result = subprocess.run([self.binary,case],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    os.environ['AGENT_TEST_MODE'] = '1'
    unittest.main()
