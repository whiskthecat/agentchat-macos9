#include <OpenTransport.h>
#include <OpenTransportProviders.h>
#include <OSUtils.h>
#include <stdio.h>
#include <string.h>
#include "AgentChatConfig.h"
#include "RelayDiscovery.h"

#ifndef mc68000
static EndpointRef endpoint;
static UInt32 started, lastProbe, serial;
static UInt16 discoveryPort;
static char nonce[9];
static long lastError;

/* A strict, length-bounded parser: stale replies, partial/oversize datagrams,
 * extra fields and malformed ports cannot become saved connection settings. */
static Boolean parse_reply(const char *bytes, long length, UInt32 address,
                           UInt16 sourcePort, DiscoveredRelay *relay)
{
    char expected[40], *end;
    unsigned long ports[2];
    long pos, i;
    unsigned int octet = (unsigned int)(address >> 24);
    if (!octet || octet >= 224 || sourcePort != discoveryPort) return FALSE;
    sprintf(expected, "AGENTCHAT_RELAY 1 %s ", nonce);
    pos = (long)strlen(expected);
    if (length <= pos || length > 80 || memcmp(bytes, expected, pos)) return FALSE;
    end = (char *)bytes + length;
    for (i = 0; i < 2; i++) {
        ports[i] = 0;
        if (bytes + pos == end || bytes[pos] < '0' || bytes[pos] > '9') return FALSE;
        while (bytes + pos < end && bytes[pos] >= '0' && bytes[pos] <= '9') {
            ports[i] = ports[i] * 10 + bytes[pos++] - '0';
            if (ports[i] > 65535) return FALSE;
        }
        if (!ports[i] || bytes + pos == end || bytes[pos++] != (i ? '\n' : ' ')) return FALSE;
    }
    if (bytes + pos != end) return FALSE;
    sprintf(relay->host, "%u.%u.%u.%u", octet, (unsigned int)((address >> 16) & 255),
            (unsigned int)((address >> 8) & 255), (unsigned int)(address & 255));
    relay->chatPort = (UInt16)ports[0]; relay->updatePort = (UInt16)ports[1];
    return TRUE;
}

static void probe(InetHost host)
{
    InetAddress address;
    TUnitData data;
    char request[48];
    memset(&data, 0, sizeof data);
    OTInitInetAddress(&address, discoveryPort, host); /* PPC is network order. */
    data.addr.buf = (UInt8 *)&address;
    data.addr.len = sizeof address;
    sprintf(request, "AGENTCHAT_DISCOVER 1 %s\n", nonce);
    data.udata.buf = (UInt8 *)request;
    data.udata.len = strlen(request);
    OTSndUData(endpoint, &data); /* nonblocking; the second probe retries loss */
}
#endif

Boolean RelayDiscoveryActive(void)
{
#ifdef mc68000
    return FALSE;
#else
    return endpoint != 0;
#endif
}

long RelayDiscoveryLastError(void)
{
#ifdef mc68000
    return 0;
#else
    return lastError;
#endif
}

void RelayDiscoveryCancel(void)
{
#ifndef mc68000
    if (endpoint) OTCloseProvider(endpoint);
    endpoint = 0;
#endif
}

Boolean RelayDiscoveryStart(UInt16 port)
{
#ifdef mc68000
    return FALSE;
#else
    OSStatus error;
    TBind binding;
    TOptMgmt option;
    TOption broadcast; /* TOption already contains value[1]. */
    RelayDiscoveryCancel();
    lastError = 0;
    endpoint = OTOpenEndpoint(OTCreateConfiguration(kUDPName), 0, NULL, &error);
    if (!endpoint || error != noErr) { lastError = error; RelayDiscoveryCancel(); return FALSE; }
    /* IP_BROADCAST is read-only while T_UNBND (Inside Macintosh: OT, IP
     * options). Bind first, then negotiate the four-byte option value. */
    memset(&binding, 0, sizeof binding);
    error = OTBind(endpoint, &binding, NULL);
    if (error != noErr) { lastError = error; RelayDiscoveryCancel(); return FALSE; }
    memset(&broadcast, 0, sizeof broadcast);
    broadcast.len = sizeof broadcast;
    broadcast.level = INET_IP;
    broadcast.name = IP_BROADCAST;
    broadcast.value[0] = T_YES;
    memset(&option, 0, sizeof option);
    option.opt.buf = (UInt8 *)&broadcast;
    option.opt.len = option.opt.maxlen = sizeof broadcast;
    option.flags = T_NEGOTIATE;
    error = OTOptionManagement(endpoint, &option, &option);
    if (error != noErr || broadcast.status != T_SUCCESS) {
        lastError = error ? error : (long)broadcast.status;
        RelayDiscoveryCancel(); return FALSE;
    }
    error = OTSetNonBlocking(endpoint);
    if (error != noErr) {
        lastError = error;
        RelayDiscoveryCancel(); return FALSE;
    }
    discoveryPort = port;
    started = TickCount(); lastProbe = started - 60;
    /* Correlation only, not authentication. LAN discovery is for trusted LANs. */
    sprintf(nonce, "%08lx", (unsigned long)(started ^ (UInt32)endpoint ^ ++serial));
    return TRUE;
#endif
}

short RelayDiscoveryPoll(DiscoveredRelay *relay)
{
#ifdef mc68000
    return -1;
#else
    UInt32 now = TickCount();
    short i;
    if (!endpoint) return -1;
    if (now - started >= 4u * 60u) { RelayDiscoveryCancel(); return -1; }
    if (now - started < 2u * 60u && now - lastProbe >= 60u) {
        lastProbe = now;
        probe((InetHost)0xffffffffUL);
#ifdef AGENTCHAT_DEV_FIXTURE
        /* QEMU user networking cannot carry a LAN broadcast. This is confined
         * to the recording VM lane; production never probes this address. */
        probe((InetHost)0x0a000202UL);
#endif
    }
    for (i = 0; i < 8; i++) {
        InetAddress source;
        TUnitData data;
        OTFlags flags = 0;
        OSStatus error;
        char reply[128];
        memset(&source, 0, sizeof source); memset(&data, 0, sizeof data);
        data.addr.buf = (UInt8 *)&source; data.addr.maxlen = sizeof source;
        data.udata.buf = (UInt8 *)reply; data.udata.maxlen = sizeof reply;
        error = OTRcvUData(endpoint, &data, &flags);
        if (error == kOTNoDataErr) break;
        if (error == kOTLookErr && OTLook(endpoint) == T_UDERR) {
            OTRcvUDErr(endpoint, NULL); continue;
        }
        if (error != noErr) { RelayDiscoveryCancel(); return -1; }
        if (!flags && data.addr.len == sizeof source && source.fAddressType == AF_INET &&
            parse_reply(reply, data.udata.len, source.fHost, source.fPort, relay)) {
            RelayDiscoveryCancel(); return 1;
        }
    }
    return 0;
#endif
}
