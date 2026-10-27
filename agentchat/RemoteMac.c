/* Native OS 9 remote inspection. All Toolbox work runs on the app event thread.
 * Absolute HFS paths, bounded chunks, synchronous replies and staged fork writes.
 * Existing files are exchanged atomically; their other fork and Finder info stay
 * intact. A disconnected client or a 60-second idle timeout removes staging.
 */
#include <Files.h>
#include <Folders.h>
#include <Sound.h>
#ifndef mc68000
#include <OpenTransport.h>
#include <OpenTransportProviders.h>
#endif
#include <Processes.h>
#include <Memory.h>
#include <Gestalt.h>
#include <OSUtils.h>
#include <Timer.h>
#include <Windows.h>
#include <QDOffscreen.h>
#include <Quickdraw.h>
#include <Script.h>
#include <AppleEvents.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "RemoteMac.h"
#include "HardwareInfo.h"
#include "IdleMetrics.h"
#include "CatalogInfo.h"

#define CHUNK 3072
#define JSON_CAP 32768
#define SCREEN_CHUNK 12288
#define MAX_SCREEN_BYTES (16L * 1024L * 1024L)
#define PACK_SOURCE_BYTES 32767L
#define PACK_BUFFER_BYTES (PACK_SOURCE_BYTES + (PACK_SOURCE_BYTES + 126L) / 127L + 16L)
#define MAX_FILE (16L * 1024L * 1024L)
#define MAX_STREAM_FILE (64L * 1024L * 1024L)
static RemoteMacSendProc gSend;
static RemoteMacStateProc gState;
static RemoteMacActionProc gAction;
static RemoteApplicationIdleProc gApplicationIdle;
static void *gApplicationIdleContext;
static char gJSON[JSON_CAP], gWire[JSON_CAP * 2];
static long gJN;
/* One prepared response, never an operation to be repeated. gWire is reserved
 * until the transport accepts the entire response or the connection resets. */
static long gReplyBytes;
static Boolean gHandling;
static Boolean gOverflow;
static const char k64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static struct { UInt32 ticks; char kind[20], detail[100]; } gEvents[64];
static unsigned long gEventSeq;
static struct {
    Boolean active, existed, resource, stream, binary;
    FSSpec target, temp;
    SInt16 fd;
    long received, size, oldData, oldResource, limit;
    UInt32 modified, used;
    UInt32 dataCRC, resourceCRC, writtenCRC, expectedWrittenCRC;
    char token[48];
} gWrite;
static struct {
    Ptr buffer;
    long remaining;
    Boolean failed;
    char nonce[41];
} gBinary;
static GWorldPtr gScreen;
static Handle gScreenPacked;
static long gScreenPackedBytes;
static UInt32 gScreenUsed;
static unsigned long gScreenToken;
static unsigned long gSerial;
static UInt32 gLastOperation;

static void add(const char *s) {
    long n = strlen(s);
    if (gJN + n >= JSON_CAP) { gOverflow = TRUE; return; }
    memcpy(gJSON + gJN, s, n + 1); gJN += n;
}
static void number(long n) { char b[32]; sprintf(b, "%ld", n); add(b); }
static void unsigned_number(unsigned long n) { char b[32]; sprintf(b, "%lu", n); add(b); }
static void quoted(const char *s) {
    const unsigned char *p = (const unsigned char *)s;
    add("\"");
    while (*p) {
        char b[8];
        if (*p == '"' || *p == '\\') { b[0] = '\\'; b[1] = *p; b[2] = 0; }
        else if (*p < 32) sprintf(b, "\\u%04x", *p);
        else { b[0] = *p; b[1] = 0; }
        add(b); p++;
    }
    add("\"");
}
static long encode(const unsigned char *p, long n, char *out) {
    long i, o = 0;
    for (i = 0; i < n; i += 3) {
        unsigned long v = (unsigned long)p[i] << 16;
        if (i + 1 < n) v |= (unsigned long)p[i + 1] << 8;
        if (i + 2 < n) v |= p[i + 2];
        out[o++] = k64[(v >> 18) & 63]; out[o++] = k64[(v >> 12) & 63];
        out[o++] = i + 1 < n ? k64[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < n ? k64[v & 63] : '=';
    }
    out[o] = 0; return o;
}
static long decode(const char *s, unsigned char *out, long cap) {
    long n = strlen(s), i, o = 0;
    if (n % 4) return -1;
    for (i = 0; i < n; i += 4) {
        long v = 0; short j, pad = 0;
        for (j = 0; j < 4; j++) {
            const char *p;
            if (s[i+j] == '=') { if (j < 2 || i + 4 != n) return -1; pad++; v <<= 6; }
            else {
                if (pad || !(p = strchr(k64, s[i+j]))) return -1;
                v = (v << 6) | (p - k64);
            }
        }
        if (o + 3 - pad > cap) return -1;
        out[o++] = (v >> 16) & 255;
        if (pad < 2) out[o++] = (v >> 8) & 255;
        if (!pad) out[o++] = v & 255;
    }
    return o;
}
static Boolean integer(const char *s, long *n) {
    const char *p = s; long v = 0;
    if (!*p) return FALSE;
    while (*p) {
        if (*p < '0' || *p > '9' || v > 214748364L || (v == 214748364L && *p > '7')) return FALSE;
        v = v * 10 + *p++ - '0'; if (v < 0) return FALSE;
    }
    *n = v; return TRUE;
}
static void pstring(const char *s, StringPtr p) { p[0] = strlen(s); memcpy(p+1, s, p[0]); }
static void cstring(ConstStr255Param p, char *s) { memcpy(s, p+1, p[0]); s[p[0]] = 0; }
static OSErr spec(const char *path, FSSpec *f) {
    Str255 p; const char *c;
    long n = strlen(path);
    if (!n || n > 255 || path[0] == ':' || !(c = strchr(path, ':')) || c == path || strstr(path, "::")) return paramErr;
    pstring(path, p); return FSMakeFSSpec(0, 0, p, f);
}
static OSErr info(const FSSpec *f, CInfoPBRec *pb) {
    memset(pb, 0, sizeof *pb);
    pb->hFileInfo.ioNamePtr = (StringPtr)f->name;
    pb->hFileInfo.ioVRefNum = f->vRefNum;
    pb->hFileInfo.ioDirID = f->parID;
    return PBGetCatInfoSync(pb);
}
static void item(CInfoPBRec *pb, const char *name) {
    Boolean folder = (pb->hFileInfo.ioFlAttrib & 16) != 0;
    char type[5], creator[5];
    add("{\"name\":"); quoted(name); add(",\"directory\":"); add(folder ? "true" : "false");
    add(",\"created_mac\":"); unsigned_number(pb->hFileInfo.ioFlCrDat);
    add(",\"modified_mac\":"); unsigned_number(pb->hFileInfo.ioFlMdDat);
    if (!folder) {
        add(",\"data_bytes\":"); number(pb->hFileInfo.ioFlLgLen);
        add(",\"resource_bytes\":"); number(pb->hFileInfo.ioFlRLgLen);
        memcpy(type, &pb->hFileInfo.ioFlFndrInfo.fdType, 4); type[4] = 0;
        memcpy(creator, &pb->hFileInfo.ioFlFndrInfo.fdCreator, 4); creator[4] = 0;
        add(",\"type\":"); quoted(type); add(",\"creator\":"); quoted(creator);
        add(",\"finder_flags\":"); number(pb->hFileInfo.ioFlFndrInfo.fdFlags);
        add(",\"locked\":"); add(pb->hFileInfo.ioFlAttrib & 1 ? "true" : "false");
    }
    add("}");
}
static OSErr volumes(void) {
    HParamBlockRec pb; Str255 name; char text[256]; short i; OSErr err;
    add("{\"volumes\":[");
    for (i = 1; i <= 32; i++) {
        memset(&pb, 0, sizeof pb); pb.volumeParam.ioNamePtr = name; pb.volumeParam.ioVolIndex = i;
        err = PBHGetVInfoSync(&pb);
        if (err == nsvErr) break;
        if (err) return err;
        if (i > 1) add(","); cstring(name, text);
        add("{\"name\":"); quoted(text); add(",\"free_bytes\":");
        number((long)pb.volumeParam.ioVFrBlk * pb.volumeParam.ioVAlBlkSiz);
        add(",\"vref\":"); number(pb.volumeParam.ioVRefNum);
        add(",\"locked\":"); add(pb.volumeParam.ioVAtrb & 0x8080 ? "true" : "false"); add("}");
    }
    add("]}"); return noErr;
}
static OSErr directory(const char *path, long start, long count) {
    FSSpec f; CInfoPBRec pb; Str255 name; char text[256]; long dir, i; OSErr err;
    err = spec(path, &f); if (err) return err;
    err = info(&f, &pb); if (err) return err;
    if (!(pb.hFileInfo.ioFlAttrib & 16)) return dirNFErr;
    dir = pb.dirInfo.ioDrDirID;
    if (start < 1 || start > 32767 || count < 1 || count > 32) return paramErr;
    add("{\"entries\":[");
    for (i = start; i < start + count && i <= 32767; i++) {
        memset(&pb, 0, sizeof pb); pb.hFileInfo.ioNamePtr = name;
        pb.hFileInfo.ioVRefNum = f.vRefNum; pb.hFileInfo.ioDirID = dir; pb.hFileInfo.ioFDirIndex = i;
        err = PBGetCatInfoSync(&pb);
        if (err == fnfErr) break;
        if (err) return err;
        if (i > start) add(","); cstring(name, text); item(&pb, text);
    }
    add("],\"next_index\":"); if (err == fnfErr || i > 32767) add("null"); else number(i);
    add("}"); return noErr;
}
/* Folder Manager resolves the active blessed System Folder (including user
 * redirection). These inventories show enabled-on-disk items, not proof an
 * INIT ran or is still resident. No private INIT/interrupt chains are walked. */
static OSErr folder_path(OSType selector, char *path) {
    short volume; long dir; OSErr err; CInfoPBRec pb; HParamBlockRec vol;
    Str255 name; char leaf[256], tail[256] = "";
    err = FindFolder(kOnSystemDisk, selector, FALSE, &volume, &dir);
    if (err) return err;
    while (dir != fsRtDirID) {
        memset(&pb, 0, sizeof pb); pb.dirInfo.ioNamePtr = name;
        pb.dirInfo.ioVRefNum = volume; pb.dirInfo.ioDrDirID = dir; pb.dirInfo.ioFDirIndex = -1;
        err = PBGetCatInfoSync(&pb); if (err) return err;
        cstring(name, leaf);
        if (strlen(leaf) + strlen(tail) + 2 > 255) return bdNamErr;
        strcpy(path, leaf); strcat(path, ":"); strcat(path, tail); strcpy(tail, path);
        if (pb.dirInfo.ioDrParID == dir) return paramErr;
        dir = pb.dirInfo.ioDrParID;
    }
    memset(&vol, 0, sizeof vol); vol.volumeParam.ioNamePtr = name; vol.volumeParam.ioVRefNum = volume;
    err = PBHGetVInfoSync(&vol); if (err) return err; cstring(name, leaf);
    if (strlen(leaf) + strlen(tail) + 2 > 255) return bdNamErr;
    strcpy(path, leaf); strcat(path, ":"); strcat(path, tail); return noErr;
}
static OSType selector_code(const char *text) {
    const unsigned char *p = (const unsigned char *)text;
    return (OSType)p[0] << 24 | (OSType)p[1] << 16 | (OSType)p[2] << 8 | p[3];
}
static void gestalt_fact(const char *key, OSType selector) {
    long value = 0; OSErr err = Gestalt(selector, &value);
    quoted(key); add(":"); if (err) add("null"); else unsigned_number((unsigned long)value);
}
static OSErr system_probe(const char *kind, const char *selector) {
    long value = 0; OSErr err; char hex[16], path[256]; OSType code;
    if (strlen(selector) == 4) code = selector_code(selector);
    else if (strlen(selector) == 10 && selector[0] == '0' && selector[1] == 'x') {
        short i;
        for (i = 2; i < 10; i++) if (!strchr("0123456789abcdefABCDEF", selector[i])) return paramErr;
        code = strtoul(selector + 2, NULL, 16);
    } else return paramErr;
    add("{\"selector\":"); quoted(selector);
    if (!strcmp(kind, "gestalt")) {
        err = Gestalt(code, &value); add(",\"status\":"); number(err);
        add(",\"value\":"); if (err) add("null"); else number(value);
        add(",\"unsigned_value\":"); if (err) add("null"); else unsigned_number((unsigned long)value);
        add(",\"hex\":"); if (err) add("null"); else { sprintf(hex, "0x%08lX", (unsigned long)value); quoted(hex); }
    } else if (!strcmp(kind, "folder")) {
        err = folder_path(code, path); add(",\"status\":"); number(err);
        add(",\"path\":"); if (err) add("null"); else quoted(path);
    } else return paramErr;
    add("}"); return noErr;
}
static OSErr system_items(const char *selector, long start, long count) {
    char path[256]; OSErr err;
    /* Public Folder Manager selectors only, never creating a missing folder. */
    if (strlen(selector) != 4) return paramErr;
    err = folder_path(selector_code(selector), path); if (err) return err;
    return directory(path, start, count);
}
static void network_interface(void) {
#ifndef mc68000
    InetInterfaceInfo info; OSStatus err; char address[32];
    memset(&info, 0, sizeof info); info.fVersion = kInetInterfaceInfoVersion;
    err = OTInetGetInterfaceInfo(&info, kDefaultInetInterface);
    add("\"default_interface\":{\"status\":"); number(err);
    if (!err) {
        OTInetHostToString(info.fAddress, address); add(",\"address\":"); quoted(address);
        OTInetHostToString(info.fNetmask, address); add(",\"netmask\":"); quoted(address);
        OTInetHostToString(info.fDefaultGatewayAddr, address); add(",\"gateway\":"); quoted(address);
        OTInetHostToString(info.fDNSAddr, address); add(",\"dns\":"); quoted(address);
        add(",\"mtu\":"); unsigned_number(info.fIfMTU);
    }
    add("}");
#else
    add("\"default_interface\":null");
#endif
}
static OSErr system_configuration(void) {
    long vm = 0, backing = 0, size = 0, level = 0; OSErr vmErr, err;
    SMStatus sound; char path[256]; unsigned short i;
    static const struct { const char *name; OSType selector; } folders[] = {
        {"system", kSystemFolderType}, {"extensions", kExtensionFolderType},
        {"control_panels", kControlPanelFolderType}, {"startup_items", kStartupFolderType},
        {"disabled_extensions", kExtensionDisabledFolderType},
        {"disabled_control_panels", kControlPanelDisabledFolderType}
    };
    vmErr = Gestalt(gestaltVMAttr, &vm);
    add("{\"memory\":{\"virtual_memory_enabled\":");
    if (vmErr) add("null"); else add(vm & (1L << gestaltVMPresent) ? "true" : "false");
    add(","); gestalt_fact("vm_attributes", gestaltVMAttr); add(",");
    gestalt_fact("physical_ram_bytes", gestaltPhysicalRAMSize); add(",");
    gestalt_fact("logical_ram_bytes", gestaltLogicalRAMSize); add(",");
    gestalt_fact("disk_cache_bytes", gestaltDiskCacheSize); add(",");
    gestalt_fact("page_size_bytes", 'pgsz');
    add(",\"backing_store_bytes\":");
    err = Gestalt(gestaltVMBackingStoreFileRefNum, &backing);
    if (vmErr || !(vm & 1) || err || GetEOF((short)backing, &size)) add("null"); else number(size);
    add(",\"temporary_free_bytes\":"); number(TempFreeMem());
    add("},\"system\":{"); gestalt_fact("version_bcd", gestaltSystemVersion); add(",");
    gestalt_fact("rom_version", gestaltROMVersion); add(","); gestalt_fact("os_attributes", gestaltOSAttr); add(",");
    gestalt_fact("mmu_type", gestaltMMUType); add(","); gestalt_fact("memory_map_attributes", gestaltMemoryMapAttr);
    add("},\"network\":{"); gestalt_fact("open_transport_attributes", gestaltOpenTpt); add(",");
    gestalt_fact("open_transport_version", gestaltOpenTptVersions); add(",");
    gestalt_fact("network_setup_version", gestaltOpenTptNetworkSetupVersion); add(","); network_interface();
    add("},\"sound\":{\"output_volume_raw\":"); err = GetDefaultOutputVolume(&level);
    if (err) add("null"); else unsigned_number(level);
    add(",\"beep_volume_raw\":"); err = GetSysBeepVolume(&level); if (err) add("null"); else unsigned_number(level);
    memset(&sound, 0, sizeof sound); err = SndManagerStatus(sizeof sound, &sound);
    add(",\"status\":"); number(err); add(",\"channels\":"); if (err) add("null"); else number(sound.smNumChannels);
    add(",\"current_cpu_load_raw\":"); if (err) add("null"); else number(sound.smCurCPULoad);
    add("},\"folders\":{");
    for (i = 0; i < sizeof folders / sizeof folders[0]; i++) {
        if (i) add(","); quoted(folders[i].name); add(":{\"status\":");
        err = folder_path(folders[i].selector, path); number(err); add(",\"path\":");
        if (err) add("null"); else quoted(path); add("}");
    }
    add("},\"limitations\":[\"System items are enabled-on-disk, not a resident extension list; boot-time Shift, failures and subsequent changes can differ.\","
        "\"No safe public API enumerates every interrupt/VBL/Time Manager/deferred task or attributes network-driver CPU time.\","
        "\"Cache size/state may be available as exact Name Registry properties; absence is unknown.\"]}");
    return noErr;
}

static OSErr read_file(const char *path, long offset, long count, Boolean resource) {
    FSSpec f; SInt16 fd; OSErr err, closed; long size, n; unsigned char data[CHUNK]; char b64[CHUNK*2];
    if (count < 1 || count > CHUNK) return paramErr;
    err = spec(path, &f); if (err) return err;
    err = resource ? FSpOpenRF(&f, fsRdPerm, &fd) : FSpOpenDF(&f, fsRdPerm, &fd); if (err) return err;
    err = GetEOF(fd, &size);
    if (!err && offset > size) err = eofErr;
    if (!err) err = SetFPos(fd, fsFromStart, offset);
    n = count;
    if (!err) { err = FSRead(fd, &n, data); if (err == eofErr) err = noErr; }
    closed = FSClose(fd); if (!err) err = closed; if (err) return err;
    encode(data, n, b64);
    add("{\"data\":"); quoted(b64); add(",\"offset\":"); number(offset);
    add(",\"bytes\":"); number(n); add(",\"total\":"); number(size);
    add(",\"eof\":"); add(offset + n >= size ? "true" : "false"); add("}"); return noErr;
}
static void cancel_write(void) {
    if (gBinary.buffer) DisposePtr(gBinary.buffer);
    memset(&gBinary, 0, sizeof gBinary);
    if (!gWrite.active) return;
    if (gWrite.fd) FSClose(gWrite.fd);
    FSpDelete(&gWrite.temp); memset(&gWrite, 0, sizeof gWrite);
}
/* Long preservation scans/copies also cooperate, without receiving commands
 * or consuming key/mouse events while a File Manager transaction is active. */
static void file_yield(UInt32 *last) {
    UInt32 now = TickCount(); EventRecord event;
    if ((UInt32)(now - *last) < 2u) return;
    WaitNextEvent(updateMask | activMask | osMask, &event, 0, NULL);
    if (gApplicationIdle) gApplicationIdle(&event, gApplicationIdleContext);
    *last = TickCount();
}
static OSErr copy_fork(const FSSpec *from, const FSSpec *to, Boolean resource, long limit) {
    SInt16 a, b; OSErr err, closed; long total = 0; Ptr buf = NewPtr(65536L); UInt32 yielded = TickCount();
    if (!buf) return memFullErr;
    err = resource ? FSpOpenRF(from, fsRdPerm, &a) : FSpOpenDF(from, fsRdPerm, &a);
    if (err) { DisposePtr(buf); return err; }
    err = resource ? FSpOpenRF(to, fsWrPerm, &b) : FSpOpenDF(to, fsWrPerm, &b);
    if (err) { FSClose(a); DisposePtr(buf); return err; }
    while (1) {
        long n = 65536L, written; err = FSRead(a, &n, buf);
        if (err && err != eofErr) break;
        if (n > limit - total) { err = paramErr; break; }
        written = n; if (n) { OSErr w = FSWrite(b, &written, buf); if (w || written != n) { err = w ? w : dskFulErr; break; } }
        total += n; if (err == eofErr || !n) { err = noErr; break; }
        file_yield(&yielded);
    }
    closed = FSClose(b); FSClose(a); DisposePtr(buf); if (!err) err = closed; return err;
}
static UInt32 crc_update(UInt32 crc, const unsigned char *data, long n) {
    static UInt32 table[256]; static Boolean ready = FALSE;
    long i; short bit;
    if (!ready) {
        for (i = 0; i < 256; i++) {
            UInt32 v = i;
            for (bit = 0; bit < 8; bit++) v = (v >> 1) ^ ((v & 1) ? 0xEDB88320UL : 0);
            table[i] = v;
        }
        ready = TRUE;
    }
    for (i = 0; i < n; i++) crc = (crc >> 8) ^ table[(crc ^ data[i]) & 255];
    return crc;
}
static Boolean crc_value(const char *text, UInt32 *value) {
    short i; UInt32 crc = 0;
    if (strlen(text) != 8) return FALSE;
    for (i = 0; i < 8; i++) {
        unsigned char c = text[i];
        if (c >= '0' && c <= '9') c -= '0';
        else if (c >= 'a' && c <= 'f') c = c - 'a' + 10;
        else return FALSE;
        crc = (crc << 4) | c;
    }
    *value = crc; return TRUE;
}
static OSErr fork_crc(const FSSpec *f, Boolean resource, UInt32 *value, long limit) {
    SInt16 fd; OSErr err, closed; Ptr data = NewPtr(65536L); long total = 0;
    UInt32 crc = 0xFFFFFFFFUL, yielded = TickCount();
    if (!data) return memFullErr;
    err = resource ? FSpOpenRF(f, fsRdPerm, &fd) : FSpOpenDF(f, fsRdPerm, &fd);
    if (err) { DisposePtr(data); return err; }
    while (1) {
        long n = 65536L; err = FSRead(fd, &n, data);
        if (err && err != eofErr) break;
        if (n > limit - total) { err = paramErr; break; }
        total += n; crc = crc_update(crc, (const unsigned char *)data, n);
        if (err == eofErr || !n) { err = noErr; break; }
        file_yield(&yielded);
    }
    closed = FSClose(fd); DisposePtr(data); if (!err) err = closed;
    *value = crc ^ 0xFFFFFFFFUL; return err;
}
static OSErr begin_write(const char *path, long size, Boolean resource, Boolean createOnly, const char *expected, Boolean stream) {
    OSErr err; FInfo finder; CInfoPBRec pb; char name[32]; UInt32 expectedCRC = 0;
    long limit = stream ? MAX_STREAM_FILE : MAX_FILE;
    if (gWrite.active) return fBsyErr;
    if (size < 0 || size > limit) return paramErr;
    if (stream || (expected[0] && strcmp(expected, "-"))) {
        if (!crc_value(expected, &expectedCRC)) return paramErr;
    }
    memset(&gWrite, 0, sizeof gWrite);
    gWrite.stream = stream; gWrite.limit = limit;
    gWrite.writtenCRC = 0xFFFFFFFFUL; gWrite.expectedWrittenCRC = expectedCRC;
    err = spec(path, &gWrite.target);
    if (err && err != fnfErr) return err;
    gWrite.existed = err == noErr;
    if (gWrite.existed && createOnly) return dupFNErr;
    if (gWrite.existed) {
        err = info(&gWrite.target, &pb); if (err) return err;
        if (pb.hFileInfo.ioFlAttrib & 17) return permErr;
        gWrite.modified = pb.hFileInfo.ioFlMdDat;
        gWrite.oldData = pb.hFileInfo.ioFlLgLen; gWrite.oldResource = pb.hFileInfo.ioFlRLgLen;
        finder = pb.hFileInfo.ioFlFndrInfo;
        err = fork_crc(&gWrite.target, FALSE, &gWrite.dataCRC, limit);
        if (!err) err = fork_crc(&gWrite.target, TRUE, &gWrite.resourceCRC, limit);
        if (err) return err;
        if (!stream && expected[0] && strcmp(expected, "-") &&
            expectedCRC != (resource ? gWrite.resourceCRC : gWrite.dataCRC)) return fBsyErr;
    } else { memset(&finder, 0, sizeof finder); finder.fdType = 'TEXT'; finder.fdCreator = 'ttxt'; }
    gWrite.temp = gWrite.target;
    sprintf(name, "AC~%08lX%04lX", (unsigned long)TickCount(), (++gSerial) & 65535);
    pstring(name, (StringPtr)gWrite.temp.name);
    err = FSpCreate(&gWrite.temp, finder.fdCreator, finder.fdType, smSystemScript); if (err) return err;
    gWrite.active = TRUE; gWrite.resource = resource; gWrite.size = size; gWrite.used = TickCount();
    strcpy(gWrite.token, name);
    if (gWrite.existed) {
        err = copy_fork(&gWrite.target, &gWrite.temp, !resource, limit);
        if (!err) err = FSpSetFInfo(&gWrite.temp, &finder);
    }
    if (!err) err = resource ? FSpOpenRF(&gWrite.temp, fsWrPerm, &gWrite.fd) : FSpOpenDF(&gWrite.temp, fsWrPerm, &gWrite.fd);
    if (err) { cancel_write(); return err; }
    add("{\"token\":"); quoted(gWrite.token); add("}"); return noErr;
}
static OSErr write_chunk(const char *token, long offset, const char *data) {
    unsigned char bytes[CHUNK]; long n, written; OSErr err;
    if (!gWrite.active || strcmp(token, gWrite.token)) return paramErr;
    n = decode(data, bytes, CHUNK);
    if (n < 0 || offset != gWrite.received || n > gWrite.size - offset) return paramErr;
    written = n; err = FSWrite(gWrite.fd, &written, bytes);
    if (err || written != n) { cancel_write(); return err ? err : dskFulErr; }
    if (gWrite.stream) gWrite.writtenCRC = crc_update(gWrite.writtenCRC, bytes, n);
    gWrite.received += n; gWrite.used = TickCount();
    add("{\"received\":"); number(gWrite.received); add("}"); return noErr;
}
/* Header and body belong to this connection's owned stage. Do not recover
 * line mode from a rejected header, failed write, timeout or disconnect. */
OSErr RemoteMacBinaryBegin(char *header) {
    char *token, *count, *nonce, *end; long size; short i;
    if (strncmp(header, "B:raw-fork-v1\t", 14)) return paramErr;
    token = header + 14;
    count = strchr(token, '\t'); if (!count) return paramErr; *count++ = 0;
    nonce = strchr(count, '\t'); if (!nonce) return paramErr; *nonce++ = 0;
    end = strchr(nonce, '\t'); if (end) return paramErr;
    if (strlen(nonce) != 32) return paramErr;
    for (i = 0; i < 32; i++) if (!((nonce[i] >= '0' && nonce[i] <= '9') || (nonce[i] >= 'a' && nonce[i] <= 'f'))) return paramErr;
    if (!integer(count, &size) || size <= 0 || size > MAX_STREAM_FILE ||
        !gWrite.active || !gWrite.stream || !gWrite.binary || gWrite.received ||
        size != gWrite.size || strcmp(token, gWrite.token) || gBinary.remaining) return paramErr;
    gBinary.buffer = NewPtr(65536L);
    if (!gBinary.buffer) return memFullErr;
    gBinary.remaining = size; strcpy(gBinary.nonce, nonce);
    gWrite.used = TickCount();
    return noErr;
}
long RemoteMacBinaryRemaining(void) { return gBinary.remaining; }
char *RemoteMacBinaryBuffer(void) { return gBinary.buffer; }
Boolean RemoteMacBinaryFailed(void) { return gBinary.failed; }
OSErr RemoteMacBinaryReceive(const char *bytes, long count) {
    long written = count, n; OSErr err;
    if (!gWrite.active || !gBinary.remaining || count <= 0 || count > gBinary.remaining || count > 65536L) return paramErr;
    err = FSWrite(gWrite.fd, &written, bytes);
    if (err || written != count) { cancel_write(); return err ? err : dskFulErr; }
    gWrite.writtenCRC = crc_update(gWrite.writtenCRC, (const unsigned char *)bytes, count);
    gWrite.received += count; gBinary.remaining -= count; gWrite.used = TickCount();
    gLastOperation = gWrite.used | 1;
    if (!gBinary.remaining) {
        DisposePtr(gBinary.buffer); gBinary.buffer = NULL;
        gHandling = TRUE;
        gJN = 0; gJSON[0] = 0; gOverflow = FALSE;
        add("{\"received\":"); number(gWrite.received); add("}");
        sprintf(gWire, "E:%s\t0\t", gBinary.nonce);
        n = strlen(gWire); n += encode((unsigned char *)gJSON, gJN, gWire + n); gWire[n++] = '\n';
        gReplyBytes = n;
        if (gSend && gSend(gWire, n)) gReplyBytes = 0;
        gHandling = FALSE;
    }
    return noErr;
}
static OSErr commit_write(const char *token) {
    OSErr err; CInfoPBRec pb; FSSpec target = gWrite.target; UInt32 crc, writtenCRC;
    Boolean cleanupWarning = FALSE, stream = gWrite.stream;
    long size, written = gWrite.received; char crcText[9];
    if (!gWrite.active || strcmp(token, gWrite.token) || gWrite.received != gWrite.size) return paramErr;
    writtenCRC = gWrite.writtenCRC ^ 0xFFFFFFFFUL;
    err = GetEOF(gWrite.fd, &size);
    if (!err && (size != gWrite.size || (stream && writtenCRC != gWrite.expectedWrittenCRC))) err = ioErr;
    if (!err) { err = FSClose(gWrite.fd); gWrite.fd = 0; }
    if (!err) err = FlushVol(NULL, target.vRefNum);
    if (!err && gWrite.existed) {
        err = info(&target, &pb);
        if (!err && (pb.hFileInfo.ioFlMdDat != gWrite.modified || pb.hFileInfo.ioFlLgLen != gWrite.oldData ||
                     pb.hFileInfo.ioFlRLgLen != gWrite.oldResource)) err = fBsyErr;
        if (!err) { err = fork_crc(&target, FALSE, &crc, gWrite.limit); if (!err && crc != gWrite.dataCRC) err = fBsyErr; }
        if (!err) { err = fork_crc(&target, TRUE, &crc, gWrite.limit); if (!err && crc != gWrite.resourceCRC) err = fBsyErr; }
        if (!err) err = FSpExchangeFiles(&gWrite.temp, &target);
        if (!err) cleanupWarning = FSpDelete(&gWrite.temp) != noErr;
    } else if (!err) err = FSpRename(&gWrite.temp, target.name);
    if (err) { cancel_write(); return err; }
    memset(&gWrite, 0, sizeof gWrite);
    FlushVol(NULL, target.vRefNum); add("{\"committed\":true,\"cleanup_warning\":");
    add(cleanupWarning ? "true" : "false");
    if (stream) {
        add(",\"written\":"); number(written);
        sprintf(crcText, "%08lx", (unsigned long)writtenCRC);
        add(",\"crc32\":"); quoted(crcText);
    }
    add("}"); return noErr;
}
/* No timer or yield: two bounded Process Manager scans suffice for interval
 * accounting. TickCount units and processActiveTime are nominal 1/60 second.
 * Reading this operation must not arm the RPC burst polling window. */
static OSErr processes(void) {
    ProcessSerialNumber psn = {0, kNoProcess}, front;
    ProcessInfoRec pi; Str255 name; char text[256], type[5], creator[5];
    OSErr err, frontErr; short i = 0; UInt32 start = TickCount();
    UnsignedWide begin, end; Boolean truncated = FALSE, isFront;
    Microseconds(&begin); frontErr = GetFrontProcess(&front);
    add("{\"sample_start_ticks\":"); unsigned_number(start);
    add(",\"ticks_per_second\":60,\"front_process_status\":"); number(frontErr);
    add(",\"processes\":[");
    while ((err = GetNextProcess(&psn)) == noErr) {
        if (i >= 40) { truncated = TRUE; break; }
        memset(&pi, 0, sizeof pi); pi.processInfoLength = sizeof pi; pi.processName = name;
        err = GetProcessInformation(&psn, &pi); if (err) return err;
        if (i++) add(","); cstring(name, text);
        memcpy(type, &pi.processType, 4); type[4] = 0;
        memcpy(creator, &pi.processSignature, 4); creator[4] = 0;
        add("{\"name\":"); quoted(text); add(",\"high\":"); unsigned_number(psn.highLongOfPSN);
        add(",\"low\":"); unsigned_number(psn.lowLongOfPSN);
        add(",\"sample_ticks\":"); unsigned_number(TickCount());
        add(",\"partition_bytes\":"); unsigned_number(pi.processSize);
        add(",\"free_bytes\":"); unsigned_number(pi.processFreeMem);
        add(",\"base_address\":"); unsigned_number((unsigned long)pi.processLocation);
        add(",\"launch_ticks\":"); unsigned_number(pi.processLaunchDate);
        add(",\"active_ticks\":"); unsigned_number(pi.processActiveTime);
        add(",\"mode\":"); unsigned_number(pi.processMode);
        add(",\"background_only\":"); add(pi.processMode & modeOnlyBackground ? "true" : "false");
        isFront = !frontErr && psn.highLongOfPSN == front.highLongOfPSN && psn.lowLongOfPSN == front.lowLongOfPSN;
        add(",\"frontmost\":"); add(frontErr ? "null" : isFront ? "true" : "false");
        add(",\"background\":"); add(frontErr ? "null" : isFront ? "false" : "true");
        add(",\"type\":"); quoted(type); add(",\"creator\":"); quoted(creator);
        add(",\"launcher_high\":"); unsigned_number(pi.processLauncher.highLongOfPSN);
        add(",\"launcher_low\":"); unsigned_number(pi.processLauncher.lowLongOfPSN); add("}");
    }
    Microseconds(&end);
    add("],\"sample_end_ticks\":"); unsigned_number(TickCount());
    add(",\"scan_us\":"); unsigned_number(end.lo - begin.lo);
    add(",\"truncated\":"); add(truncated ? "true" : "false");
    add(",\"accounting\":\"Process Manager accumulated active ticks; nominal 60 Hz, coarse scheduling accounting; not an interrupt profiler\"}");
    return err == procNotFound || truncated ? noErr : err;
}
static OSErr application_command(long high, long low, const char *encoded) {
    ProcessSerialNumber psn;
    RemoteApplicationReply result;
    char command[REMOTE_APPLICATION_COMMAND_MAX + 1]; long n;
    memset(&result, 0, sizeof result);
    n = decode(encoded, (unsigned char *)command, REMOTE_APPLICATION_COMMAND_MAX);
    if (n <= 0 || memchr(command, 0, n)) {
        result.error = paramErr;
        strcpy(result.detail, "Application command must contain 1..1024 ASCII JSON bytes");
    } else {
        command[n] = 0;
        psn.highLongOfPSN = high; psn.lowLongOfPSN = low;
        RemoteApplicationsCommand(&psn, command, n, gApplicationIdle, gApplicationIdleContext, &result);
    }
    /* This wrapper stays a successful RPC envelope even when the application
     * rejects the command: errn is a full signed long, not the OSErr-sized
     * generic mac_error. The host reports the detail and never retries it.
     */
    add("{\"error\":"); number(result.error);
    add(",\"application_error\":"); number(result.applicationError);
    add(",\"detail\":"); quoted(result.detail);
    add(",\"elapsed\":"); unsigned_number(result.elapsedTicks);
    add(",\"reply\":"); quoted(result.text); add("}");
    return noErr;
}
/* Classic applications share a logical address space. Read only the live
 * application partition reported by Process Manager, never caller-chosen
 * arbitrary pointers, ROM, I/O or a stale cached partition. No memory writes.
 * The current application's event-thread context allows normal VM paging.
 */
static OSErr process_info(long high, long low) {
    ProcessSerialNumber psn, front; ProcessInfoRec pi; Str255 name;
    char text[256], type[5], creator[5]; Boolean isFront = FALSE; OSErr err;
    psn.highLongOfPSN = high; psn.lowLongOfPSN = low;
    memset(&pi, 0, sizeof pi); pi.processInfoLength = sizeof pi; pi.processName = name;
    err = GetProcessInformation(&psn, &pi); if (err) return err;
    if (!GetFrontProcess(&front)) SameProcess(&psn, &front, &isFront);
    cstring(name, text); memcpy(type, &pi.processType, 4); type[4] = 0;
    memcpy(creator, &pi.processSignature, 4); creator[4] = 0;
    add("{\"name\":"); quoted(text); add(",\"high\":"); number(high); add(",\"low\":"); number(low);
    add(",\"base_address\":"); unsigned_number((unsigned long)pi.processLocation);
    add(",\"partition_bytes\":"); unsigned_number(pi.processSize);
    add(",\"free_bytes\":"); unsigned_number(pi.processFreeMem);
    add(",\"launch_ticks\":"); unsigned_number(pi.processLaunchDate);
    add(",\"active_ticks\":"); unsigned_number(pi.processActiveTime);
    add(",\"mode\":"); unsigned_number(pi.processMode);
    add(",\"type\":"); quoted(type); add(",\"creator\":"); quoted(creator);
    add(",\"frontmost\":"); add(isFront ? "true}" : "false}"); return noErr;
}
static OSErr process_read(const char *id, long offset, long count) {
    ProcessSerialNumber psn; ProcessInfoRec pi; OSErr err;
    char key[64], *colon, b64[CHUNK*2]; long high, low;
    unsigned long base, size; unsigned char bytes[CHUNK];
    if (strlen(id) >= sizeof key || count < 1 || count > CHUNK) return paramErr;
    strcpy(key, id); colon = strchr(key, ':'); if (!colon) return paramErr;
    *colon++ = 0; if (!integer(key, &high) || !integer(colon, &low)) return paramErr;
    psn.highLongOfPSN = high; psn.lowLongOfPSN = low;
    memset(&pi, 0, sizeof pi); pi.processInfoLength = sizeof pi;
    err = GetProcessInformation(&psn, &pi); if (err) return err;
    base = (unsigned long)pi.processLocation; size = pi.processSize;
    if (!base || base + size < base || (unsigned long)offset >= size ||
        (unsigned long)count > size - (unsigned long)offset) return paramErr;
    memcpy(bytes, pi.processLocation + offset, count); encode(bytes, count, b64);
    add("{\"data\":"); quoted(b64); add(",\"bytes\":"); number(count);
    add(",\"base_address\":"); unsigned_number(base);
    add(",\"address\":"); unsigned_number(base + offset);
    add(",\"partition_bytes\":"); unsigned_number(size);
    add(",\"launch_ticks\":"); unsigned_number(pi.processLaunchDate); add("}"); return noErr;
}
static OSErr windows(void) {
    WindowPtr w; GrafPtr saved; short i = 0; Str255 title; char text[256];
    add("{\"scope\":\"AgentChat application windows\",\"windows\":["); GetPort(&saved);
    for (w = FrontWindow(); w && i < 32; w = (WindowPtr)((WindowPeek)w)->nextWindow) {
        Point origin = {0, 0}; Rect r = w->portRect;
        SetPort(w); LocalToGlobal(&origin); GetWTitle(w, title); cstring(title, text);
        if (i++) add(","); add("{\"title\":"); quoted(text);
        add(",\"left\":"); number(origin.h); add(",\"top\":"); number(origin.v);
        add(",\"width\":"); number(r.right-r.left); add(",\"height\":"); number(r.bottom-r.top);
        add(",\"visible\":"); add(((WindowPeek)w)->visible ? "true" : "false"); add("}");
    }
    SetPort(saved); add("]}"); return noErr;
}
static OSErr state(void) {
    RemoteMacDiagnostics d; long os = 0, machine = 0; Rect r = qd.screenBits.bounds;
    ProcessSerialNumber psn; ProcessInfoRec pi;
    memset(&d, 0, sizeof d); gState(&d);
    Gestalt(gestaltSystemVersion, &os); Gestalt(gestaltMachineType, &machine);
    add("{\"version\":"); quoted(d.version); add(",\"status\":"); quoted(d.status);
    add(",\"model\":"); quoted(d.model); add(",\"conversation\":"); quoted(d.conversation);
    add(",\"connected\":"); add(d.connected ? "true" : "false"); add(",\"busy\":"); add(d.busy ? "true" : "false");
    add(",\"history_loading\":"); add(d.loading ? "true" : "false");
    add(",\"test_build\":"); add(d.testBuild ? "true" : "false");
    add(",\"transcript_rows\":"); number(d.transcriptRows); add(",\"draft_bytes\":"); number(d.draftBytes);
    add(",\"send_queue_bytes\":"); number(d.queueBytes);
    add(",\"draft_image\":"); quoted(d.draftImage ? d.draftImage : "");
    add(",\"image_uploading\":"); add(d.imageUploading ? "true" : "false");
    add(",\"draft_image_ready\":"); add(d.imageReady ? "true" : "false");
    add(",\"model_accepts_images\":"); add(d.modelImages ? "true" : "false");
    add(",\"transcript_image_rows\":"); number(d.imageRows);
    add(",\"transcript_buffered\":"); add(d.transcriptBuffered ? "true" : "false");
    add(",\"transcript_frames\":"); unsigned_number(d.transcriptFrames);
    add(",\"window_frames\":"); unsigned_number(d.windowFrames);
    add(",\"navigation_conversation_top\":"); number(d.conversationTop);
    add(",\"model_rows\":"); number(d.modelRows);
    add(",\"question_id\":"); quoted(d.questionID);
    add(",\"question_index\":"); number(d.questionIndex);
    add(",\"question_custom_bytes\":"); number(d.questionCustomBytes);
    add(",\"question_edit_bytes\":"); number(d.questionEditBytes);
    add(",\"current_zone_free_bytes\":"); number(FreeMem());
    add(",\"largest_current_zone_block\":"); number(MaxBlock());
    memset(&pi, 0, sizeof pi); pi.processInfoLength = sizeof pi;
    if (!GetCurrentProcess(&psn) && !GetProcessInformation(&psn, &pi)) {
        add(",\"application_partition_bytes\":"); unsigned_number(pi.processSize);
        add(",\"application_free_bytes\":"); unsigned_number(pi.processFreeMem);
    }
    add(",\"system_version_bcd\":"); number(os);
    add(",\"machine_type\":"); number(machine); add(",\"screen_width\":"); number(r.right-r.left);
    add(",\"screen_height\":"); number(r.bottom-r.top); add("}"); return noErr;
}
static OSErr events(void) {
    unsigned long i, start = gEventSeq > 64 ? gEventSeq - 64 : 0;
    add("{\"events\":[");
    for (i = start; i < gEventSeq; i++) {
        if (i > start) add(","); add("{\"ticks\":"); number(gEvents[i%64].ticks);
        add(",\"kind\":"); quoted(gEvents[i%64].kind); add(",\"detail\":"); quoted(gEvents[i%64].detail); add("}");
    }
    add("]}"); return noErr;
}
static void release_screen(void) {
    if (gScreen) DisposeGWorld(gScreen); gScreen = NULL;
    if (gScreenPacked) DisposeHandle(gScreenPacked); gScreenPacked = NULL;
    gScreenPackedBytes = 0;
}
/* PackBits is a native QuickDraw service with a signed-short source length.
 * Four byte planes compress flat desktop colors much better than interleaved
 * XRGB. Concatenated blocks are ordinary PackBits packets with no new headers.
 * Preserve every snapshot byte, including row padding; host reinterleaves it.
 */
static void pack_screen(PixMapHandle pm, long total) {
    Ptr input = NULL, packed = NULL; unsigned char *source;
    long planeBytes = total / 4, capacity = total + total / 64 + 1024;
    long used = 0, at, n, i, encoded; short plane; Boolean failed = FALSE;
    if (total < 4 || total > MAX_SCREEN_BYTES || total % 4) return;
    input = NewPtr(PACK_SOURCE_BYTES); packed = NewPtr(PACK_BUFFER_BYTES);
    if (!input || !packed) goto cleanup;
    gScreenPacked = NewHandle(capacity); if (!gScreenPacked) goto cleanup;
    if (!LockPixels(pm)) { failed = TRUE; goto cleanup; }
    source = (unsigned char *)GetPixBaseAddr(pm); HLock(gScreenPacked);
    for (plane = 0; plane < 4 && !failed; plane++) {
        for (at = 0; at < planeBytes; at += n) {
            Ptr src = input, dst = packed;
            n = planeBytes - at; if (n > PACK_SOURCE_BYTES) n = PACK_SOURCE_BYTES;
            for (i = 0; i < n; i++) input[i] = source[(at + i) * 4 + plane];
            PackBits(&src, &dst, (short)n); encoded = dst - packed;
            if (encoded < 1 || encoded > PACK_BUFFER_BYTES || encoded > capacity - used) { failed = TRUE; break; }
            memcpy(*gScreenPacked + used, packed, encoded); used += encoded;
            SystemTask();
        }
    }
    HUnlock(gScreenPacked); UnlockPixels(pm);
    if (!failed && used < total) {
        SetHandleSize(gScreenPacked, used);
        if (MemError() == noErr) gScreenPackedBytes = used;
    }
cleanup:
    if (input) DisposePtr(input); if (packed) DisposePtr(packed);
    if (!gScreenPackedBytes && gScreenPacked) { DisposeHandle(gScreenPacked); gScreenPacked = NULL; }
}
static OSErr screen_begin(const char *transport) {
    CGrafPtr saved; GDHandle device; PixMapHandle pm, source; Rect r = qd.screenBits.bounds; OSErr err;
    long total;
    release_screen();
    err = NewGWorld(&gScreen, 32, &r, NULL, NULL, 0); if (err) return err;
    pm = GetGWorldPixMap(gScreen); source = (*GetMainDevice())->gdPMap;
    total = ((*pm)->rowBytes & 0x3FFF) * (r.bottom-r.top);
    if (total < 4 || total > MAX_SCREEN_BYTES) { release_screen(); return paramErr; }
    if (!LockPixels(pm)) { release_screen(); return memFullErr; }
    memset(GetPixBaseAddr(pm), 0, total);
    GetGWorld(&saved, &device); SetGWorld(gScreen, NULL);
    ForeColor(blackColor); BackColor(whiteColor);
    CopyBits((BitMap *)*source, (BitMap *)*pm, &r, &r, srcCopy, NULL);
    SetGWorld(saved, device); UnlockPixels(pm);
    if (!strcmp(transport, "packbits-planar-xrgb")) pack_screen(pm, total);
    gScreenUsed = TickCount();
    add("{\"width\":"); number(r.right-r.left); add(",\"height\":"); number(r.bottom-r.top);
    add(",\"stride\":"); number((*pm)->rowBytes & 0x3FFF); add(",\"format\":\"XRGB\",\"token\":");
    { char token[32]; gScreenToken = ++gSerial; sprintf(token, "%lu", gScreenToken); quoted(token); }
    if (!strcmp(transport, "packbits-planar-xrgb")) {
        add(",\"transport\":"); quoted(gScreenPacked ? "packbits-planar-xrgb" : "raw");
        add(",\"transport_bytes\":"); number(gScreenPacked ? gScreenPackedBytes : total);
    }
    add("}"); return noErr;
}
static OSErr screen_read(const char *token, long offset, long count) {
    PixMapHandle pm; long total; char expected[32], b64[SCREEN_CHUNK*2];
    sprintf(expected, "%lu", gScreenToken);
    if (!gScreen || strcmp(token, expected) || count < 1 || count > SCREEN_CHUNK) return paramErr;
    pm = GetGWorldPixMap(gScreen); total = gScreenPacked ? gScreenPackedBytes : ((*pm)->rowBytes & 0x3FFF) * ((*pm)->bounds.bottom - (*pm)->bounds.top);
    if (offset >= total) return eofErr;
    if (count > total - offset) count = total - offset;
    if (gScreenPacked) {
        HLock(gScreenPacked); encode((unsigned char *)*gScreenPacked + offset, count, b64); HUnlock(gScreenPacked);
    } else {
        if (!LockPixels(pm)) return memFullErr;
        encode((unsigned char *)GetPixBaseAddr(pm) + offset, count, b64); UnlockPixels(pm);
    }
    gScreenUsed = TickCount();
    add("{\"data\":"); quoted(b64); add(",\"total\":"); number(total); add("}"); return noErr;
}

void RemoteMacInit(RemoteMacSendProc send, RemoteMacStateProc stateProc, RemoteMacActionProc action) {
    gSend = send; gState = stateProc; gAction = action;
}
void RemoteMacSetApplicationIdle(RemoteApplicationIdleProc idle, void *context) {
    gApplicationIdle = idle; gApplicationIdleContext = context;
}
void RemoteMacEvent(const char *kind, const char *detail) {
    unsigned long i = gEventSeq++ % 64;
    gEvents[i].ticks = TickCount(); strncpy(gEvents[i].kind, kind, sizeof gEvents[i].kind - 1);
    strncpy(gEvents[i].detail, detail, sizeof gEvents[i].detail - 1);
}
void RemoteMacReset(void) {
    gReplyBytes = 0;
    cancel_write(); release_screen();
}
Boolean RemoteMacCanReceive(void) {
    return !gHandling && !gReplyBytes;
}
long RemoteMacPendingReplyBytes(void) {
    return gReplyBytes;
}
void RemoteMacTick(void) {
    if (gHandling) return;
    if (gReplyBytes && gSend) {
        /* Sending must not permit a notifier/idle callback to dispatch another
         * operation or retry this same response recursively. */
        gHandling = TRUE;
        if (gSend(gWire, gReplyBytes)) gReplyBytes = 0;
        gHandling = FALSE;
    }
    if (gBinary.remaining && (UInt32)(TickCount() - gWrite.used) >= 15u * 60u) {
        cancel_write(); gBinary.failed = TRUE;
    } else if (gWrite.active && TickCount() - gWrite.used > 60u * 60u) cancel_write();
    if (gScreen && TickCount() - gScreenUsed > 60u * 60u) release_screen();
}
static OSErr set_file_info(const char *path, long flags, Boolean hasFlags, const char *encoded) {
    FSSpec fs; FInfo finder; CInfoPBRec pb; unsigned char bytes[8]; OSErr err;
    if (decode(encoded, bytes, 8) != 8 || flags > 65535) return paramErr;
    err = spec(path, &fs); if (!err) err = FSpGetFInfo(&fs, &finder); if (err) return err;
    memcpy(&finder.fdType, bytes, 4); memcpy(&finder.fdCreator, bytes + 4, 4);
    if (hasFlags) finder.fdFlags = flags;
    finder.fdFlags &= ~0x0100; /* Finder must re-read type/creator and its icon. */
    err = FSpSetFInfo(&fs, &finder); if (!err) err = info(&fs, &pb); if (!err) item(&pb, path);
    return err;
}
/* Trial installers may recreate an application. Restore the recorded catalog
 * dates and exact flags after its authenticated forks are back in place. Keep
 * the remaining catalog fields and never clear Finder's initialization bit. */
static UInt32 catalog_u32(const unsigned char *p) {
    return ((UInt32)p[0] << 24) | ((UInt32)p[1] << 16) | ((UInt32)p[2] << 8) | p[3];
}
static OSErr restore_file_info(const char *path, const char *encoded) {
    FSSpec fs; CInfoPBRec pb; unsigned char bytes[10]; OSErr err;
    if (decode(encoded, bytes, sizeof bytes) != sizeof bytes) return paramErr;
    err = spec(path, &fs);
    if (!err) err = AgentRestoreCatalog(&fs, ((UInt16)bytes[0] << 8) | bytes[1],
        catalog_u32(bytes + 2), catalog_u32(bytes + 6), &pb);
    if (!err) item(&pb, path);
    return err;
}
static OSErr open_document(const char *path) {
    FSSpec fs; OSType finder = 'MACS'; OSErr err; AEAddressDesc target = {typeNull,NULL};
    AppleEvent event = {typeNull,NULL}; AEDescList files = {typeNull,NULL};
    err = spec(path, &fs);
    if (!err) err = AECreateDesc(typeApplSignature, &finder, sizeof finder, &target);
    if (!err) err = AECreateAppleEvent(kCoreEventClass, kAEOpenDocuments, &target, kAutoGenerateReturnID, kAnyTransactionID, &event);
    if (!err) err = AECreateList(NULL, 0, FALSE, &files);
    if (!err) err = AEPutPtr(&files, 1, typeFSS, &fs, sizeof fs);
    if (!err) err = AEPutParamDesc(&event, keyDirectObject, &files);
    if (!err) err = AESend(&event, NULL, kAENoReply | kAENeverInteract, kAENormalPriority, kAEDefaultTimeout, NULL, NULL);
    AEDisposeDesc(&files); AEDisposeDesc(&event); AEDisposeDesc(&target);
    if (!err) add("{\"submitted\":true,\"handler\":\"Finder\"}");
    return err;
}

/* A host stream of small operations (a file import is thousands of 3 KB
 * chunks) must not wait out the event loop's idle sleep between each one. */

Boolean RemoteMacBusy(void) { return gLastOperation && TickCount() - gLastOperation < 30u; }

Boolean RemoteMacHandle(char *command) {
    char *f[9], path[256], data[CHUNK+1]; unsigned char decoded[256];
    short i; long offset, count, n; OSErr err = noErr; FSSpec fs;
    if (strncmp(command, "RPC\t", 4)) return FALSE;
    /* The receive owner must retain the command when this is FALSE. This
     * defensive check also prevents idle callbacks from replacing gWire. */
    if (!RemoteMacCanReceive()) return FALSE;
    f[0] = command + 4;
    for (i = 1; i < 9; i++) { char *p = strchr(f[i-1], '\t'); if (!p) return TRUE; *p = 0; f[i] = p + 1; }
    if (!f[0][0] || strlen(f[0]) > 40) return TRUE;
    for (i = 0; f[0][i]; i++) if (!((f[0][i] >= '0' && f[0][i] <= '9') ||
                                  (f[0][i] >= 'a' && f[0][i] <= 'f'))) return TRUE;
    gHandling = TRUE;
    if (strcmp(f[1], "idle_metrics") && strcmp(f[1], "processes")) gLastOperation = TickCount() | 1;
    gJN = 0; gJSON[0] = 0; gOverflow = FALSE;
    n = decode(f[2], decoded, 255);
    if (n < 0 || memchr(decoded, 0, n) || !integer(f[3], &offset) || !integer(f[4], &count)) err = paramErr;
    else { memcpy(path, decoded, n); path[n] = 0; }
    RemoteMacEvent("rpc", f[1]);
    if (!err) {
        if (!strcmp(f[1], "volumes")) err = volumes();
        else if (!strcmp(f[1], "list")) err = directory(path, offset, count);
        else if (!strcmp(f[1], "stat")) { CInfoPBRec pb; err = spec(path, &fs); if (!err) err = info(&fs, &pb); if (!err) item(&pb, path); }
        else if (!strcmp(f[1], "set_info")) err = set_file_info(path, offset, count != 0, f[5]);
        else if (!strcmp(f[1], "restore_info")) err = restore_file_info(path, f[5]);
        else if (!strcmp(f[1], "open")) err = open_document(path);
        else if (!strcmp(f[1], "read")) err = read_file(path, offset, count, !strcmp(f[6], "resource"));
        else if (!strcmp(f[1], "write_begin")) err = begin_write(path, count, !strcmp(f[6], "resource"), !strcmp(f[7], "1"), f[8], FALSE);
        else if (!strcmp(f[1], "write_stream_begin")) {
            if ((strcmp(f[6], "data") && strcmp(f[6], "resource")) || (strcmp(f[7], "0") && strcmp(f[7], "1"))) err = paramErr;
            else err = begin_write(path, count, !strcmp(f[6], "resource"), !strcmp(f[7], "1"), f[8], TRUE);
            /* Old relays send an empty data field; old clients ignore the
             * offer and return only a token. Never infer support by version. */
            if (!err && !strcmp(f[5], "cmF3LWZvcmstdjE=")) {
                gWrite.binary = TRUE; gJSON[--gJN] = 0;
                add(",\"transport\":\"raw-fork-v1\"}");
            }
        }
        else if (!strcmp(f[1], "write_chunk")) err = write_chunk(path, offset, f[5]);
        else if (!strcmp(f[1], "write_commit")) err = commit_write(path);
        else if (!strcmp(f[1], "write_cancel")) { if (!gWrite.active || strcmp(path, gWrite.token)) err = paramErr; else { cancel_write(); add("{\"cancelled\":true}"); } }
        else if (!strcmp(f[1], "mkdir")) { long dir; err = spec(path, &fs); if (err == fnfErr) err = FSpDirCreate(&fs, smSystemScript, &dir); else if (!err) err = dupFNErr; if (!err) add("{\"created\":true}"); }
        else if (!strcmp(f[1], "rename")) { Str255 name; n = decode(f[5], (unsigned char *)data, 31); if (n <= 0 || memchr(data, ':', n) || memchr(data, 0, n)) err = paramErr; else { data[n] = 0; pstring(data, name); err = spec(path, &fs); if (!err) err = FSpRename(&fs, name); if (!err) add("{\"renamed\":true}"); } }
        else if (!strcmp(f[1], "delete")) { err = spec(path, &fs); if (!err) err = FSpDelete(&fs); if (!err) add("{\"deleted\":true}"); }
        else if (!strcmp(f[1], "state")) err = state();
        else if (!strcmp(f[1], "system")) err = system_configuration();
        else if (!strcmp(f[1], "system_items")) err = system_items(path, offset, count);
        else if (!strcmp(f[1], "system_probe")) err = system_probe(f[6], path);
        else if (!strcmp(f[1], "registry_probe")) {
            err = HardwareRegistryProbeJSON(path, f[6], gJSON, JSON_CAP);
            if (!err) gJN = strlen(gJSON);
        }
        else if (!strcmp(f[1], "idle_metrics")) {
            err = IdleMetricsJSON(gJSON, JSON_CAP);
            if (!err) gJN = strlen(gJSON);
        }
        else if (!strcmp(f[1], "hardware")) {
            err = HardwareInfoJSON(gJSON, JSON_CAP);
            if (!err) gJN = strlen(gJSON);
        }
        else if (!strcmp(f[1], "events")) err = events();
        else if (!strcmp(f[1], "processes")) err = processes();
        else if (!strcmp(f[1], "process_info")) err = process_info(offset, count);
        else if (!strcmp(f[1], "application_command")) err = application_command(offset, count, f[5]);
        else if (!strcmp(f[1], "process_read")) err = process_read(path, offset, count);
        else if (!strcmp(f[1], "windows")) err = windows();
        else if (!strcmp(f[1], "screen_begin")) err = screen_begin(path);
        else if (!strcmp(f[1], "screen_read")) err = screen_read(path, offset, count);
        else if (!strcmp(f[1], "screen_end")) { release_screen(); add("{\"released\":true}"); }
        else if (!strcmp(f[1], "focus")) { ProcessSerialNumber psn; psn.highLongOfPSN = offset; psn.lowLongOfPSN = count; err = SetFrontProcess(&psn); if (!err) add("{\"focused\":true}"); }
        else if (!strcmp(f[1], "launch")) { LaunchParamBlockRec pb; err = spec(path, &fs); if (!err) { memset(&pb, 0, sizeof pb); pb.launchBlockID = extendedBlock; pb.launchEPBLength = extendedBlockLen; pb.launchAppSpec = &fs; pb.launchControlFlags = launchContinue | launchNoFileFlags; err = LaunchApplication(&pb); } if (!err) add("{\"launched\":true}"); }
        else if (!strcmp(f[1], "client")) { n = decode(f[5], (unsigned char *)data, CHUNK); if (n < 0 || memchr(data, 0, n)) err = paramErr; else { data[n] = 0; err = gAction(path, data); if (!err) add("{\"done\":true}"); } }
        else if (!strcmp(f[1], "palette")) { CTabHandle ct = GetCTable(8); if (!ct) err = memFullErr; else { short indexes[2] = {215,255}; add("{\"entries\":["); for(i=0;i<2;i++){ ColorSpec cs = (**ct).ctTable[indexes[i]]; if(i)add(","); add("{\"index\":");number(indexes[i]);add(",\"r\":");number(cs.rgb.red);add(",\"g\":");number(cs.rgb.green);add(",\"b\":");number(cs.rgb.blue);add("}"); } add("]}"); DisposeCTable(ct); } }
        else err = paramErr;
    }
    if (gOverflow) err = memFullErr;
    if (err) { gJN = 0; gJSON[0] = 0; add("{\"error\":\"Macintosh Toolbox operation failed\",\"mac_error\":"); number(err); add("}"); }
    sprintf(gWire, "E:%s\t%d\t", f[0], err ? 1 : 0);
    n = strlen(gWire); n += encode((unsigned char *)gJSON, gJN, gWire+n); gWire[n++] = '\n';
    gReplyBytes = n;
    if (gSend && gSend(gWire, n)) gReplyBytes = 0;
    gHandling = FALSE;
    return TRUE;
}
