#include "HardwareInfo.h"
#include <Gestalt.h>
#include <Quickdraw.h>
#ifndef mc68000
#include <NameRegistry.h>
#endif
#include <stdio.h>
#include <string.h>

static char *gOut;
static long gCapacity, gUsed;
static Boolean gFull;
static void put(const char *text) {
    long n = strlen(text);
    if (gUsed + n >= gCapacity) { gFull = true; return; }
    memcpy(gOut + gUsed, text, n + 1); gUsed += n;
}
static void number(unsigned long n) { char b[32]; sprintf(b, "%lu", n); put(b); }
static void signed_number(long n) { char b[32]; sprintf(b, "%ld", n); put(b); }
static void quote(const unsigned char *s, long n) {
    long i; char b[8]; put("\"");
    for (i = 0; i < n; i++) {
        if (s[i] == '"' || s[i] == '\\') { b[0] = '\\'; b[1] = s[i]; b[2] = 0; put(b); }
        else if (s[i] < 32 || s[i] >= 127) { sprintf(b, "\\u%04x", s[i]); put(b); }
        else { b[0] = s[i]; b[1] = 0; put(b); }
    }
    put("\"");
}
static void fact(const char *name, OSType selector) {
    long value; OSErr error = Gestalt(selector, &value);
    put("\""); put(name); put("\":");
    if (error) put("null"); else number((unsigned long)value);
}
#ifndef mc68000
static Boolean property(const RegEntryID *entry, const char *name, unsigned char *bytes, RegPropertyValueSize *size) {
    RegPropertyValueSize actual;
    if (RegistryPropertyGetSize(entry, name, &actual) || actual > 512) return false;
    *size = actual;
    return RegistryPropertyGet(entry, name, bytes, size) == noErr;
}
static void registry(void) {
    RegEntryIter iterator = NULL; RegEntryID entry; Boolean done = false;
    OSStatus error; long visited = 0, reported = 0;
    put("\"registry\":{\"entries\":[");
    error = RegistryEntryIterateCreate(&iterator);
    if (!error) while (!done && visited < 1024 && reported < 16) {
        unsigned char model[512], kind[512], name[512];
        RegPropertyValueSize modelSize = 0, kindSize = 0, nameSize = 0;
        Boolean hasModel, hasKind, hasName;
        error = RegistryEntryIterate(&iterator, kRegIterContinue, &entry, &done);
        if (error || done) break;
        visited++;
        hasModel = property(&entry, "model", model, &modelSize);
        hasKind = property(&entry, "device_type", kind, &kindSize);
        hasName = property(&entry, "name", name, &nameSize);
        if (hasModel || (hasKind && kindSize >= 7 && !memcmp(kind, "display", 7))
            || (hasKind && kindSize >= 3 && !memcmp(kind, "cpu", 3))
            || (hasName && nameSize >= 4 && (!memcmp(name, "ATY,", 4) || !memcmp(name, "NVDA", 4)))) {
            static const char *properties[] = { "name", "device_type", "model", "compatible",
                "vendor-id", "device-id", "revision-id", "clock-frequency", "bus-frequency",
                "VRAM,memsize", "VRAM,totalsize", "AAPL,vram-size", "vram-size",
                "ATY,memsize", "ATY,VRAM", "width", "height", "depth",
                "i-cache-size", "d-cache-size", "cache-size", "cache-line-size", "l2-cache", "timebase-frequency" };
            long i, count = 0; char path[512]; RegPathNameSize pathSize;
            if (reported++) put(",");
            put("{\"path\":");
            if (!RegistryEntryToPathSize(&entry, &pathSize) && pathSize <= sizeof(path)
                && !RegistryCStrEntryToPath(&entry, path, sizeof(path))) {
                path[sizeof(path)-1] = 0; quote((unsigned char *)path, strlen(path));
            } else put("null");
            put(",\"properties\":{");
            for (i = 0; i < (long)(sizeof(properties) / sizeof(properties[0])); i++) {
                unsigned char value[512]; RegPropertyValueSize size;
                if (property(&entry, properties[i], value, &size)) {
                    long j; char b[3];
                    if (count++) put(",");
                    quote((const unsigned char *)properties[i], strlen(properties[i])); put(":{\"hex\":\"");
                    for (j = 0; j < (long)size; j++) { sprintf(b, "%02x", value[j]); put(b); }
                    put("\"");
                    if (i <= 3) { put(",\"text\":"); quote(value, size && !value[size-1] ? size-1 : size); }
                    if (size == 4) {
                        unsigned long scalar = (unsigned long)value[0] << 24 | (unsigned long)value[1] << 16
                            | (unsigned long)value[2] << 8 | value[3];
                        put(",\"be32\":"); number(scalar);
                    }
                    put("}");
                }
            }
            put("}}");
        }
        RegistryEntryIDDispose(&entry);
    }
    if (iterator) RegistryEntryIterateDispose(&iterator);
    put("],\"status\":"); signed_number(error); put(",\"visited\":"); number(visited);
    put(",\"bounded\":"); put(!done && !error ? "true" : "false"); put("}");
}
#endif

OSErr HardwareInfoJSON(char *output, long capacity) {
    GDHandle device; long count = 0;
    if (!output || capacity < 2) return paramErr;
    gOut = output; gCapacity = capacity; gUsed = 0; gFull = false; output[0] = 0;
    put("{\"source\":\"native Toolbox/Gestalt/Name Registry\",");
    fact("machine_type", gestaltMachineType); put(",");
    fact("cpu_type", gestaltNativeCPUtype); put(",");
    fact("cpu_family", gestaltNativeCPUfamily); put(",");
    fact("cpu_hz", gestaltProcClkSpeed); put(",");
    fact("bus_hz", gestaltBusClkSpeed); put(",");
    fact("physical_ram_bytes", gestaltPhysicalRAMSize); put(",");
    fact("logical_ram_bytes", gestaltLogicalRAMSize); put(",");
    fact("system_version_bcd", gestaltSystemVersion); put(",");
    put("\"displays\":[");
    for (device = GetDeviceList(); device && count < 8; device = GetNextDevice(device)) {
        Rect bounds = (**device).gdRect;
        if (count++) put(",");
        put("{\"id\":"); signed_number((**device).gdID); put(",\"driver_ref\":"); signed_number((**device).gdRefNum);
        put(",\"main\":"); put(TestDeviceAttribute(device, mainScreen) ? "true" : "false");
        put(",\"active\":"); put(TestDeviceAttribute(device, screenActive) ? "true" : "false");
        put(",\"width\":"); signed_number(bounds.right - bounds.left);
        put(",\"height\":"); signed_number(bounds.bottom - bounds.top);
        put(",\"pixel_bits\":"); signed_number((**(**device).gdPMap).pixelSize); put("}");
    }
    put("],");
#ifndef mc68000
    registry();
#else
    put("\"registry\":{\"entries\":[],\"unavailable\":\"Name Registry requires PowerPC\"}");
#endif
    put("}");
    return gFull ? memFullErr : noErr;
}

OSErr HardwareRegistryProbeJSON(const char *path, const char *name, char *output, long capacity) {
#ifndef mc68000
    RegEntryID entry; unsigned char bytes[512]; RegPropertyValueSize size = 0; OSStatus err;
    long i; char hex[3];
    if (!path || !name || !*path || !*name || strlen(name) > 31) return paramErr;
    gOut = output; gCapacity = capacity; gUsed = 0; gFull = false; output[0] = 0;
    err = RegistryCStrEntryLookup(NULL, path, &entry);
    if (!err) {
        err = RegistryPropertyGetSize(&entry, name, &size);
        if (!err && size > sizeof bytes) err = paramErr;
        if (!err) err = RegistryPropertyGet(&entry, name, bytes, &size);
        RegistryEntryIDDispose(&entry);
    }
    put("{\"status\":"); signed_number(err); put(",\"size\":"); number(size);
    put(",\"hex\":");
    if (err) put("null"); else {
        put("\""); for (i = 0; i < (long)size; i++) { sprintf(hex, "%02x", bytes[i]); put(hex); } put("\"");
        put(",\"text_bytes\":"); quote(bytes, size);
        if (size == 4) { put(",\"be32\":"); number((unsigned long)bytes[0] << 24 | (unsigned long)bytes[1] << 16 | (unsigned long)bytes[2] << 8 | bytes[3]); }
    }
    put("}"); return gFull ? memFullErr : noErr;
#else
    (void)path; (void)name; (void)output; (void)capacity; return unimpErr;
#endif
}
