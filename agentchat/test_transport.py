#!/usr/bin/env python3
"""Run the production queue/RPC/receive functions against recording host C fakes.

No OpenTransport endpoint, Macintosh, VM, bridge, or inference process is used.
The fake operations deliberately return large screen/application/file responses.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent


def function(source, name):
    """Copy a complete production function; braces in these functions are balanced."""
    match = re.search(r"^(?:static )?(?:void|Boolean|long|OSErr|UInt32|char \*)\s*" + re.escape(name) +
                      r"\([^;]*?\)\s*\{", source, re.M)
    if not match:
        raise ValueError("Missing production function " + name)
    depth = 1
    # Braces inside JSON strings and comments do not delimit C functions.
    tokens = re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*|[{}]', source[match.end():], re.S)
    for token in tokens:
        depth += (token.group() == "{") - (token.group() == "}")
        if not depth:
            return source[match.start():match.end() + token.end()] + "\n"
    raise ValueError("Unterminated production function " + name)



PREFIX = r'''
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "RemoteMac.h"
#include "SendQueue.h"
#define mc68000 1
#define CHUNK 3072
#define JSON_CAP 32768
#define noErr 0
#define paramErr -50
#define memFullErr -108
#define dskFulErr -34
#define MAX_STREAM_FILE (64L * 1024L * 1024L)
#define AGENTCHAT_BINARY_BURST_TICKS 2u
#define fnfErr -43
#define dupFNErr -48
#define smSystemScript 0
#define extendedBlock 0
#define extendedBlockLen 0
#define launchContinue 0
#define launchNoFileFlags 0
#define kOTNoDataErr -3150
typedef uint32_t UInt32;
typedef long OTFlags;
typedef struct { long unused; } FSSpec, CInfoPBRec;
typedef unsigned char Str255[256];
typedef struct { long launchBlockID, launchEPBLength; FSSpec *launchAppSpec;
                 long launchControlFlags; } LaunchParamBlockRec;
typedef struct { unsigned short red, green, blue; } RGBColor;
typedef struct { RGBColor rgb; } ColorSpec;
typedef struct { ColorSpec ctTable[256]; } ColorTable;
typedef ColorTable **CTabHandle;
static RemoteMacSendProc gSend;
static RemoteMacStateProc gState;
static RemoteMacActionProc gAction;
static RemoteApplicationIdleProc gApplicationIdle;
static void *gApplicationIdleContext;
static char gJSON[JSON_CAP], gWire[JSON_CAP * 2];
static long gJN, gReplyBytes;
static Boolean gHandling, gOverflow;
static const char k64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
typedef char *Ptr;
static struct { Boolean active, stream, binary; unsigned long used;
    char token[48]; long received, size, fd; UInt32 writtenCRC; } gWrite;
static struct { Ptr buffer; long remaining; Boolean failed; char nonce[41]; } gBinary;
static Ptr NewPtr(long count) { return malloc((size_t)count); }
static void DisposePtr(Ptr ptr) { free(ptr); }
static char forkBody[1000000]; static long forkBytes;
static int forkError, forkShort, forkTicks;
static UInt32 fakeTicks = 1;
static OSErr FSWrite(long fd, long *count, const char *bytes) {
    (void)fd;
    if (forkError) return -34;
    if (forkShort && *count) --*count;
    assert(*count >= 0 && forkBytes + *count <= (long)sizeof(forkBody));
    memcpy(forkBody + forkBytes, bytes, (size_t)*count); forkBytes += *count;
    fakeTicks += forkTicks;
    return 0;
}
static void *gScreen;
static unsigned long gScreenUsed, gLastOperation;
static void IdleMetricsSend(void) {}
static void IdleMetricsReceive(void) {}
static char gAccum[65536], gQ[65536], gRecv[4096];
static long gAccumLen, gQLen, gQPos, gRecvLen, gRecvPos, gEP;
static Boolean gConnected, gReceiving, gBusy, gDirty, gNavDirty, gHistoryLoading, gDraftCached;
static Boolean gUsingDiscovered;
static void RelayDiscoveryCancel(void) {}
static int gConnPhase;
static Boolean gRelayHeartbeat;
static Boolean gRelayGreeted;
static UInt32 gRelayLastReceive;
static char sent[1000000], incoming[1000000], lastStatus[100];
static long sentBytes, incomingBytes, incomingPos, sendAllowance, sendSlice, receiveSlice;
static int sendFailure, receiveFailure, recvCalls, dispatchCalls, operationCalls, cleanupCalls;
static int responseBytes, sendCalls, rejectCalls, recursiveSend, probeOperation;
static char lastReply[65536];
static long lastReplyBytes;
static void poll_recv(void);
void RemoteMacEvent(const char *kind, const char *detail) { (void)kind; (void)detail; }
static UInt32 TickCount(void) { return fakeTicks; }
static void set_status(const char *msg) { snprintf(lastStatus, sizeof(lastStatus), "%s", msg); }
static void flush_cur(void) {}
static void push_line(const char *line) { (void)line; }
static void ToolQuestionsReset(void) {}
static void ChatImagesDisconnect(void) {}
static void OTRemoveNotifier(long endpoint) { (void)endpoint; }
static void OTSndOrderlyDisconnect(long endpoint) { (void)endpoint; }
static void OTCloseProvider(long endpoint) { (void)endpoint; }
static void cancel_write(void) {
    cleanupCalls++; gWrite.active = FALSE;
    if (gBinary.buffer) DisposePtr(gBinary.buffer);
    memset(&gBinary, 0, sizeof(gBinary));
}
static void release_screen(void) { cleanupCalls++; gScreen = NULL; }
static long OTSnd(long endpoint, const char *data, long bytes, int flags) {
    long count = bytes;
    (void)endpoint; (void)flags;
    if (sendFailure) return sendFailure;
    if (count > sendAllowance) count = sendAllowance;
    if (count > sendSlice) count = sendSlice;
    assert(count >= 0 && sentBytes + count <= (long)sizeof(sent));
    memcpy(sent + sentBytes, data, (size_t)count);
    sentBytes += count; sendAllowance -= count;
    return count;
}
static long OTRcv(long endpoint, char *data, long capacity, OTFlags *flags) {
    long count = incomingBytes - incomingPos;
    (void)endpoint; (void)flags; recvCalls++;
    if (receiveFailure) return receiveFailure;
    if (!count) return kOTNoDataErr;
    if (count > capacity) count = capacity;
    if (count > receiveSlice) count = receiveSlice;
    memcpy(data, incoming + incomingPos, (size_t)count); incomingPos += count;
    return count;
}
static void add(const char *s);
static OSErr operation(void) {
    char command[] = "RPC\taaaaaaaa\thardware\t\t0\t0\t\tdata\t0\t";
    operationCalls++;
    if (probeOperation) {
        assert(!RemoteMacCanReceive());
        assert(!RemoteMacHandle(command));
        assert(!strcmp(command, "RPC\taaaaaaaa\thardware\t\t0\t0\t\tdata\t0\t"));
        poll_recv(); RemoteMacTick();
    }
    memset(gJSON, 'x', (size_t)responseBytes);
    gJSON[responseBytes] = 0; gJN = responseBytes;
    return noErr;
}
static OSErr HardwareInfoJSON(char *out, long capacity) {
    assert(out == gJSON && responseBytes < capacity); return operation();
}
/* All actual native dispatch branches compile, but only recording fake
 * operations execute. Old-style argument lists let Toolbox pointers remain
 * opaque here without pretending to implement their layout or behavior. */
#define FAKE(name) static OSErr name() { return operation(); }
FAKE(volumes) FAKE(directory) FAKE(set_file_info) FAKE(open_document)
FAKE(restore_file_info) FAKE(read_file) FAKE(begin_write) FAKE(write_chunk) FAKE(commit_write)
FAKE(system_configuration) FAKE(system_items) FAKE(system_probe)
FAKE(HardwareRegistryProbeJSON) FAKE(IdleMetricsJSON)
FAKE(state) FAKE(events) FAKE(processes) FAKE(process_info)
FAKE(application_command) FAKE(process_read) FAKE(windows)
FAKE(screen_begin) FAKE(screen_read) FAKE(FSpDirCreate) FAKE(FSpRename)
FAKE(FSpDelete) FAKE(SetFrontProcess) FAKE(LaunchApplication)
static OSErr spec() { return noErr; }
static OSErr info() { return noErr; }
static void item() { add("{}"); }
static void pstring(const char *text, unsigned char *out) { out[0] = (unsigned char)strlen(text); }
static CTabHandle GetCTable(int depth) { (void)depth; return NULL; }
static void DisposeCTable(CTabHandle table) { (void)table; }
'''

SUFFIX = r'''
static OSErr action(const char *name, const char *value) {
    (void)name; (void)value; return operation();
}
static Boolean sendReply(const char *bytes, long count) {
    Boolean accepted;
    sendCalls++;
    assert(count > 0 && count <= (long)sizeof(lastReply));
    if (gReplyBytes && lastReplyBytes) {
        assert(count == lastReplyBytes && !memcmp(lastReply, bytes, (size_t)count));
    }
    memcpy(lastReply, bytes, (size_t)count); lastReplyBytes = count;
    if (recursiveSend) { RemoteMacTick(); poll_recv(); }
    if (rejectCalls) { rejectCalls--; return FALSE; }
    accepted = q_put(bytes, count);
    if (accepted) lastReplyBytes = 0;
    return accepted;
}
static void handle_line(char *line) {
    dispatchCalls++;
    if (!strncmp(line, "C:", 2)) assert(RemoteMacHandle(line + 2));
    else if (!strcmp(line, "CLOSE")) close_conn();
}
static void reset(void) {
    RemoteMacReset();
    gConnected = TRUE; gReceiving = FALSE; gEP = 1;
    fakeTicks = 1; gRelayHeartbeat = FALSE; gRelayGreeted = TRUE; gRelayLastReceive = 0;
    gAccumLen = gQLen = gQPos = gRecvLen = gRecvPos = 0;
    sentBytes = incomingBytes = incomingPos = 0;
    sendAllowance = 0; sendSlice = LONG_MAX; receiveSlice = LONG_MAX;
    sendFailure = receiveFailure = recvCalls = dispatchCalls = operationCalls = 0;
    responseBytes = sendCalls = rejectCalls = recursiveSend = probeOperation = 0;
    lastReplyBytes = 0; lastStatus[0] = 0;
    forkBytes = forkError = forkShort = forkTicks = 0;
    RemoteMacInit(sendReply, NULL, action);
}
static void receiveCommand(const char *nonce, const char *operationName) {
    incomingBytes += sprintf(incoming + incomingBytes,
        "C:RPC\t%s\t%s\t\t0\t0\t\tdata\t0\t\n", nonce, operationName);
}
static void queuePrefix(void) {
    char bytes[65536]; long index;
    reset();
    for (index = 0; index < (long)sizeof(bytes); index++) bytes[index] = (char)(index % 251);
    assert(q_put(bytes, sizeof(bytes)));
    sendAllowance = 60000; sendSlice = 113;
    assert(q_put("tail", 4));
    assert(gQLen == 5540 && gQPos == 0);
    assert(!memcmp(gQ, bytes + 60000, 5536) && !memcmp(gQ + 5536, "tail", 4));
    sendAllowance = 100000; q_flush();
    assert(gQLen == 0 && gQPos == 0 && sentBytes == 65540);
    assert(!memcmp(sent, bytes, sizeof(bytes)) && !memcmp(sent + sizeof(bytes), "tail", 4));
}
static void queueFull(void) {
    char bytes[65536]; reset(); memset(bytes, 'q', sizeof(bytes));
    assert(q_put(bytes, sizeof(bytes)));
    assert(!q_put("x", 1) && gQLen == 65536 && gQPos == 0 && sentBytes == 0);
    assert(!memcmp(gQ, bytes, sizeof(bytes)) && strstr(lastStatus, "reply waiting"));
    assert(!q_put("x", LONG_MAX) && gQLen == 65536);
    assert(!q_put(NULL, 1) && !q_put("x", -1));
    sendFailure = -3170; assert(!q_put("x", 1) && gQLen == 65536);
    sendFailure = 0; sendAllowance = 100000; sendSlice = 3;
    q_flush(); assert(sentBytes == 65536 && !memcmp(sent, bytes, sizeof(bytes)));
    close_conn(); assert(!q_put("x", 1) && !gQLen);
}
static void escapedLine(void) {
    char bytes[65536]; reset(); memset(bytes, 'q', sizeof(bytes));
    assert(q_put(bytes, sizeof(bytes)));
    sendAllowance = 10; sendSlice = 4; send_line("a\\b\nc\r");
    assert(gQLen == 65536 - 10 + 10 && gQPos == 0);
    sendAllowance = 100000; q_flush();
    assert(sentBytes == 65546 && !memcmp(sent, bytes, sizeof(bytes)));
    assert(!memcmp(sent + sizeof(bytes), "a\\\\b\\nc\\n\n", 10));
}
static void pendingReply(const char *operationName) {
    int callsBefore; char copy[65536]; long count;
    reset(); responseBytes = 24500; rejectCalls = 3; recursiveSend = probeOperation = 1;
    receiveCommand("aaaaaaaa", operationName);
    receiveCommand("bbbbbbbb", "hardware");
    poll_recv();
    assert(operationCalls == 1 && dispatchCalls == 1 && sendCalls == 1);
    assert(!RemoteMacCanReceive() && gReplyBytes > 32000 && gRecvPos < gRecvLen);
    count = gReplyBytes; memcpy(copy, gWire, (size_t)count); callsBefore = recvCalls;
    poll_recv();
    assert(sendCalls == 2 && operationCalls == 1 && recvCalls == callsBefore);
    assert(count == gReplyBytes && !memcmp(copy, gWire, (size_t)count));
    poll_recv(); assert(operationCalls == 1 && sendCalls == 3);
    /* The accepted first reply stays in the queue before the second reply. */
    responseBytes = 3; poll_recv();
    assert(RemoteMacCanReceive() && operationCalls == 2 && dispatchCalls == 2);
    assert(gQLen > count && !memcmp(copy, gQ, (size_t)count));
    assert(!memcmp(gQ + count, "E:bbbbbbbb\t0\t", 13));
    sendAllowance = 100000; sendSlice = 127; q_flush();
    assert(!gQLen && sentBytes > count && !memcmp(copy, sent, (size_t)count));
}
static void realQueueBackpressure(void) {
    char prefix[65536]; int reads;
    reset(); memset(prefix, 'p', sizeof(prefix)); assert(q_put(prefix, sizeof(prefix)));
    responseBytes = 20000; receiveCommand("aaaaaaaa", "write_commit");
    receiveCommand("bbbbbbbb", "hardware"); poll_recv();
    assert(operationCalls == 1 && !RemoteMacCanReceive() && gReplyBytes > 26000);
    reads = recvCalls; poll_recv(); assert(recvCalls == reads && operationCalls == 1);
    sendAllowance = 40000; sendSlice = 401; responseBytes = 3; poll_recv();
    assert(operationCalls == 2 && RemoteMacCanReceive());
    sendAllowance = 100000; q_flush();
    assert(!memcmp(sent, prefix, sizeof(prefix)));
    assert(!memcmp(sent + sizeof(prefix), "E:aaaaaaaa\t0\t", 13));
}
static void resetPending(void) {
    int sends;
    reset(); rejectCalls = 100; responseBytes = 500; receiveCommand("aaaaaaaa", "screen_read");
    receiveCommand("bbbbbbbb", "write_commit"); poll_recv();
    assert(!RemoteMacCanReceive() && gRecvPos < gRecvLen && operationCalls == 1);
    sends = sendCalls; close_conn();
    assert(RemoteMacCanReceive() && gRecvLen == 0 && gRecvPos == 0 && gQLen == 0);
    RemoteMacTick(); assert(sendCalls == sends);
    lastReplyBytes = 0; /* The recording belongs to the closed connection. */
    gConnected = TRUE; gEP = 2; rejectCalls = 0; incomingBytes = incomingPos = 0;
    responseBytes = 3; receiveCommand("cccccccc", "hardware"); poll_recv();
    assert(operationCalls == 2 && gQLen > 0 && !memcmp(gQ, "E:cccccccc", 10));
}
static void receiveBoundaries(void) {
    reset(); receiveSlice = 1; responseBytes = 10;
    receiveCommand("aaaaaaaa", "hardware"); receiveCommand("bbbbbbbb", "hardware");
    poll_recv(); assert(operationCalls == 2 && dispatchCalls == 2 && RemoteMacCanReceive());
    reset(); incomingBytes = sprintf(incoming, "CLOSE\nC:RPC\taaaaaaaa\thardware\t\t0\t0\t\tdata\t0\t\n");
    poll_recv(); assert(!gConnected && operationCalls == 0 && gRecvLen == 0 && !gReceiving);
    reset(); receiveFailure = -123; poll_recv();
    assert(!gConnected && !gReceiving && !gRecvLen && strstr(lastStatus, "connection lost"));
}
static void imageUploadCannotStarveReply(void) {
    char prefix[65536], image[1001]; int turn, uploads = 0; long replyBytes;
    reset(); memset(prefix, 'p', sizeof(prefix)); assert(q_put(prefix, sizeof(prefix)));
    responseBytes = 20000; receiveCommand("aaaaaaaa", "write_commit"); poll_recv();
    replyBytes = RemoteMacPendingReplyBytes(); assert(replyBytes > 26000 && q_upload_space() == 0);
    memset(image, 'i', sizeof(image) - 1); image[sizeof(image) - 1] = 0;
    for (turn = 0; turn < 200 && !RemoteMacCanReceive(); turn++) {
        sendAllowance = 512; sendSlice = 31; poll_recv(); q_flush();
        if (q_upload_space() > 1600) { send_line(image); uploads++; }
    }
    assert(turn < 200 && RemoteMacCanReceive() && RemoteMacPendingReplyBytes() == 0);
    assert(uploads == 0 && operationCalls == 1);
    sendAllowance = 5000; q_flush();
    assert(q_upload_space() > 1600); send_line(image);
    sendAllowance = 100000; q_flush();
    assert(sentBytes == (long)sizeof(prefix) + replyBytes + (long)sizeof(image));
    assert(!memcmp(sent, prefix, sizeof(prefix)));
    assert(!memcmp(sent + sizeof(prefix), "E:aaaaaaaa\t0\t", 13));
    assert(!memcmp(sent + sizeof(prefix) + replyBytes, image, sizeof(image) - 1));
    assert(sent[sentBytes - 1] == '\n');
}
static void maximumResponse(void) {
    char copy[65536]; long count;
    reset(); responseBytes = JSON_CAP - 1; rejectCalls = 1;
    receiveCommand("aaaaaaaa", "hardware"); poll_recv();
    count = RemoteMacPendingReplyBytes();
    assert(count > 43000 && count < (long)sizeof(gWire));
    memcpy(copy, gWire, (size_t)count);
    poll_recv(); assert(RemoteMacCanReceive() && gQLen == count && operationCalls == 1);
    sendAllowance = 100000; sendSlice = 17; q_flush();
    assert(sentBytes == count && !memcmp(sent, copy, (size_t)count));
}
static void manyRPCsWithPartialFlow(void) {
    int index, turns = 0; long offset = 0;
    reset(); responseBytes = 1000;
    for (index = 0; index < 500; index++) {
        char nonce[20]; sprintf(nonce, "%08x", index); receiveCommand(nonce, "hardware");
    }
    while ((incomingPos < incomingBytes || gRecvPos < gRecvLen || !RemoteMacCanReceive() || gQLen) && turns++ < 5000) {
        sendAllowance = 181; sendSlice = 7; poll_recv(); q_flush();
    }
    assert(turns < 5000 && operationCalls == 500 && dispatchCalls == 500);
    for (index = 0; index < 500; index++) {
        char header[30]; long end;
        sprintf(header, "E:%08x\t0\t", index);
        assert(!memcmp(sent + offset, header, strlen(header)));
        end = offset + (long)strlen(header) + 4 * ((responseBytes + 2) / 3);
        assert(sent[end] == '\n'); offset = end + 1;
    }
    assert(offset == sentBytes && RemoteMacCanReceive() && gRecvPos == gRecvLen);
}

static void binaryFrame(long bytes, long slice) {
    long i, header;
    reset(); sendAllowance = 100000; receiveSlice = slice;
    gWrite.active = gWrite.stream = gWrite.binary = TRUE;
    gWrite.received = 0; gWrite.size = bytes; gWrite.used = fakeTicks;
    gWrite.writtenCRC = 0xFFFFFFFFu; strcpy(gWrite.token, "AC~123456781234");
    header = sprintf(incoming, "B:raw-fork-v1\t%s\t%ld\t0123456789abcdef0123456789abcdef\n", gWrite.token, bytes);
    for (i = 0; i < bytes; i++) incoming[header + i] = (char)(i % 256);
    /* The body includes arbitrary newlines/NULs and control-shaped text. */
    if (bytes > 80) memcpy(incoming + header, "C:QUIT\nP:never dispatch\nH:alive\n\0\r", 32);
    incomingBytes = header + bytes;
}
static void binaryExact(void) {
    int i; const long slices[] = {1, 37, 4096, 65536};
    for (i = 0; i < 4; i++) {
        long bodyOffset;
        binaryFrame(200123, slices[i]); bodyOffset = incomingBytes - 200123;
        incomingBytes += sprintf(incoming + incomingBytes, "H:alive\n");
        receiveCommand("bbbbbbbb", "hardware");
        poll_recv();
        assert(gConnected && !RemoteMacBinaryRemaining() && !gBinary.buffer);
        assert(forkBytes == 200123 && !memcmp(forkBody, incoming + bodyOffset, (size_t)forkBytes));
        assert(gWrite.received == forkBytes && dispatchCalls == 2 && operationCalls == 1);
        assert(!memcmp(sent, "E:0123456789abcdef0123456789abcdef\t0\t", 37));
        assert(gWrite.writtenCRC == crc_update(0xFFFFFFFFu, (const unsigned char *)forkBody, forkBytes));
    }
    /* Standard IEEE CRC-32 check vector, across multiple updates. */
    assert((crc_update(crc_update(0xFFFFFFFFu, (const unsigned char *)"1234", 4),
                       (const unsigned char *)"56789", 5) ^ 0xFFFFFFFFu) == 0xcbf43926u);
}
static void binaryBackpressureAndYield(void) {
    long written;
    binaryFrame(10000, 4096); rejectCalls = 2;
    receiveCommand("bbbbbbbb", "hardware"); poll_recv();
    assert(forkBytes == 10000 && !RemoteMacCanReceive() && operationCalls == 0);
    written = forkBytes; poll_recv();
    assert(forkBytes == written && operationCalls == 0 && !RemoteMacCanReceive());
    poll_recv(); assert(forkBytes == written && operationCalls == 1 && RemoteMacCanReceive());
    binaryFrame(300000, 65536); forkTicks = 1;
    poll_recv();
    assert(gConnected && RemoteMacBinaryRemaining() > 0 && forkBytes <= 4096 + 65536);
    assert(fakeTicks == 3 && !gReceiving);
    while (RemoteMacBinaryRemaining()) poll_recv();
    assert(gConnected && forkBytes == 300000);
}

static void binaryTruncated(void) {
    binaryFrame(10000, 37); incomingBytes -= 500;
    poll_recv(); assert(gConnected && RemoteMacBinaryRemaining() == 500 && gWrite.active);
    fakeTicks += 900; poll_recv();
    assert(!gConnected && !gWrite.active && !gBinary.buffer && !dispatchCalls);
    /* Reconnect starts in line mode; the old token/body is never resumed. */
    incomingBytes = incomingPos = 0; gConnected = TRUE; gEP = 2;
    receiveCommand("bbbbbbbb", "hardware"); poll_recv(); assert(operationCalls == 1);
    binaryFrame(10000, 4096); incomingBytes -= 2000; poll_recv();
    receiveFailure = -123; poll_recv();
    assert(!gConnected && !gWrite.active && !gBinary.buffer && !dispatchCalls);
}
static void binaryInvalid(void) {
    int kind;
    for (kind = 0; kind < 7; kind++) {
        binaryFrame(10000, 4096);
        if (kind == 0) gWrite.size++;
        if (kind == 1) gWrite.binary = FALSE;
        if (kind == 2) gWrite.stream = FALSE;
        if (kind == 3) gWrite.token[3] = 'F';
        if (kind == 4) gWrite.received = 1;
        if (kind == 5) incoming[60] = 'z'; /* malformed nonce */
        if (kind == 6) *strchr(incoming, '\n') = '\r'; /* LF framing only */
        poll_recv(); assert(!gConnected && !gWrite.active && !forkBytes && !dispatchCalls);
    }
    binaryFrame(10000, 4096); forkShort = 1; poll_recv();
    assert(!gConnected && !gWrite.active && !dispatchCalls);
    binaryFrame(10000, 4096); forkError = 1; poll_recv();
    assert(!gConnected && !gWrite.active && !dispatchCalls);
}

static void relayWatchdog(void) {
    reset(); fakeTicks = 100000; poll_recv(); assert(gConnected); /* legacy */
    gRelayHeartbeat = TRUE; gRelayLastReceive = fakeTicks;
    fakeTicks += 2699; poll_recv(); assert(gConnected);
    fakeTicks++; poll_recv(); assert(!gConnected && !gRelayHeartbeat && !gRelayGreeted);
    assert(strstr(lastStatus, "relay timeout"));
    reset(); gRelayHeartbeat = TRUE; gRelayLastReceive = 1;
    fakeTicks = 9000; memcpy(incoming, "hello\n", 6); incomingBytes = 6;
    poll_recv(); assert(gConnected && gRelayLastReceive == fakeTicks);
    reset(); gRelayGreeted = FALSE; fakeTicks = 2700;
    poll_recv(); assert(!gConnected); /* no initial greeting */
    reset(); gRelayHeartbeat = TRUE; gRelayLastReceive = UINT32_MAX - 100;
    fakeTicks = 2598; poll_recv(); assert(gConnected);
    fakeTicks++; poll_recv(); assert(!gConnected); /* native tick wrap */
}
int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "prefix")) queuePrefix();
    else if (!strcmp(argv[1], "full")) queueFull();
    else if (!strcmp(argv[1], "escape")) escapedLine();
    else if (!strcmp(argv[1], "commit")) pendingReply("write_commit");
    else if (!strcmp(argv[1], "screen")) pendingReply("screen_read");
    else if (!strcmp(argv[1], "application")) pendingReply("application_command");
    else if (!strcmp(argv[1], "hardware")) pendingReply("hardware");
    else if (!strcmp(argv[1], "pressure")) realQueueBackpressure();
    else if (!strcmp(argv[1], "reset")) resetPending();
    else if (!strcmp(argv[1], "receive")) receiveBoundaries();
    else if (!strcmp(argv[1], "image")) imageUploadCannotStarveReply();
    else if (!strcmp(argv[1], "maximum")) maximumResponse();
    else if (!strcmp(argv[1], "many")) manyRPCsWithPartialFlow();
    else if (!strcmp(argv[1], "watchdog")) relayWatchdog();
    else if (!strcmp(argv[1], "binary")) binaryExact();
    else if (!strcmp(argv[1], "truncated")) binaryTruncated();
    else if (!strcmp(argv[1], "invalid")) binaryInvalid();
    else if (!strcmp(argv[1], "binary-pressure")) binaryBackpressureAndYield();
    else abort();
    puts("passed"); return 0;
}
'''


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="agentchat-transport-fake-")
        temp = Path(cls.scratch.name)
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("A host C compiler is required")
        (temp / "Types.h").write_text("typedef int Boolean; typedef short OSErr;\n#define TRUE 1\n#define FALSE 0\n")
        (temp / "Events.h").write_text("typedef struct { long unused; } EventRecord;\n")
        (temp / "Processes.h").write_text("typedef struct { unsigned long highLongOfPSN, lowLongOfPSN; } ProcessSerialNumber;\n")
        app = (SOURCE / "agentchat.c").read_text()
        mac = (SOURCE / "RemoteMac.c").read_text()
        c = PREFIX
        c += "\n".join(function(mac, name) for name in ["add", "number", "encode", "decode", "integer"])
        c += "\n".join(function(app, name) for name in ["q_flush", "q_put", "q_upload_space", "send_line", "close_conn"])
        c += "\n".join(function(mac, name) for name in ["RemoteMacInit", "RemoteMacSetApplicationIdle", "RemoteMacReset", "RemoteMacCanReceive", "RemoteMacPendingReplyBytes", "RemoteMacTick", "RemoteMacHandle", "crc_update", "RemoteMacBinaryBegin", "RemoteMacBinaryRemaining", "RemoteMacBinaryBuffer", "RemoteMacBinaryFailed", "RemoteMacBinaryReceive"])
        c += "static void handle_line(char *line);\n" + function(app, "poll_recv") + SUFFIX
        (temp / "transport.c").write_text(c)
        cls.binary = temp / "transport"
        command = [compiler, "-std=c99", "-Wall", "-Wextra", "-Werror", "-Wno-multichar",
                   "-I" + str(temp), "-I" + str(SOURCE), str(temp / "transport.c"), "-o", str(cls.binary)]
        if os.environ.get("AGENTCHAT_TEST_SANITIZERS") == "1":
            command[1:1] = ["-fsanitize=address,undefined", "-g"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError("Recording fake compile failed:\n" + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def scenario(self, name):
        result = subprocess.run([str(self.binary), name], check=False, capture_output=True,
                                text=True, timeout=5, env=dict(os.environ, AGENT_TEST_MODE="1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "passed")

    def test_binary_framing_every_byte_and_following_heartbeat_reply(self):
        self.scenario("binary")

    def test_binary_completed_reply_backpressure_and_bounded_yield(self):
        self.scenario("binary-pressure")

    def test_binary_truncation_idle_timeout_disconnect_and_no_resume(self):
        self.scenario("truncated")

    def test_binary_wrong_length_token_capability_nonce_disk_full_and_short_write(self):
        self.scenario("invalid")

    def test_sent_prefix_capacity_and_short_writes(self):
        self.scenario("prefix")

    def test_full_invalid_error_and_disconnected_queue_are_atomic(self):
        self.scenario("full")

    def test_escaped_command_follows_partial_prefix_in_order(self):
        self.scenario("escape")

    def test_pending_commit_is_never_executed_again(self):
        self.scenario("commit")

    def test_pending_snapshot_bytes_are_unchanged(self):
        self.scenario("screen")

    def test_pending_application_command_is_never_executed_again(self):
        self.scenario("application")

    def test_pending_hardware_reply_blocks_receive_reentry(self):
        self.scenario("hardware")

    def test_actual_full_queue_defers_reply_and_next_operation(self):
        self.scenario("pressure")

    def test_disconnect_discards_pending_reply_and_buffered_tail(self):
        self.scenario("reset")

    def test_receive_byte_boundaries_error_and_disconnect_dispatch(self):
        self.scenario("receive")

    def test_image_upload_budget_cannot_starve_pending_reply(self):
        self.scenario("image")

    def test_maximum_json_response_survives_rejection_and_short_writes(self):
        self.scenario("maximum")

    def test_many_rpcs_preserve_order_under_tiny_partial_flow(self):
        self.scenario("many")

    def test_relay_loss_quiet_legacy_greeting_buffered_receive_and_tick_wrap(self):
        self.scenario("watchdog")


if __name__ == "__main__":
    unittest.main()
