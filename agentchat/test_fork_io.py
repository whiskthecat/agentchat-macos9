#!/usr/bin/env python3
"""Production preservation copy/CRC loops against bounded File Manager fakes."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zlib

from test_transport import function

PREFIX = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef int Boolean;
typedef int16_t SInt16;
typedef int16_t OSErr;
typedef uint32_t UInt32;
typedef char *Ptr;
typedef struct { int id; } FSSpec;
typedef struct { int what; } EventRecord;
#define FALSE 0
#define TRUE 1
#define noErr 0
#define memFullErr -108
#define paramErr -50
#define dskFulErr -34
#define eofErr -39
#define ioErr -36
#define fsRdPerm 1
#define fsWrPerm 2
#define updateMask 64
#define activMask 256
#define osMask 32768
static char bodies[4][200000];
static long sizes[4], positions[4], maxRead, maxWrite;
static int allocations, failAllocation, shortWrite, diskFull, readError, openFiles, yields;
static UInt32 ticks;
static Ptr NewPtr(long count) {
    assert(count == 65536 && !allocations);
    if (failAllocation) return NULL;
    allocations++; return malloc((size_t)count);
}
static void DisposePtr(Ptr p) { assert(p && allocations == 1); allocations--; free(p); }
static UInt32 TickCount(void) { return ticks; }
static void WaitNextEvent(int mask, EventRecord *event, int sleep, void *region) {
    assert(mask == (updateMask | activMask | osMask) && sleep == 0 && !region);
    yields++; event->what = 0;
}
static Boolean idle(const EventRecord *event, void *context) { assert(!event->what && !context); return FALSE; }
static Boolean (*gApplicationIdle)(const EventRecord *, void *) = idle;
static void *gApplicationIdleContext;
static OSErr openFork(const FSSpec *file, int resource, SInt16 *fd) {
    *fd = file->id * 2 + resource; positions[*fd] = 0; openFiles++; return noErr;
}
static OSErr FSpOpenDF(const FSSpec *f, int mode, SInt16 *fd) { (void)mode; return openFork(f, 0, fd); }
static OSErr FSpOpenRF(const FSSpec *f, int mode, SInt16 *fd) { (void)mode; return openFork(f, 1, fd); }
static OSErr FSClose(SInt16 fd) { (void)fd; openFiles--; return noErr; }
static OSErr FSRead(SInt16 fd, long *count, void *buffer) {
    long wanted = *count, available = sizes[fd] - positions[fd];
    assert(wanted <= 65536); if (wanted > maxRead) maxRead = wanted; ticks++;
    if (readError) return ioErr;
    if (*count > available) *count = available;
    memcpy(buffer, bodies[fd] + positions[fd], (size_t)*count); positions[fd] += *count;
    return *count < wanted ? eofErr : noErr;
}
static OSErr FSWrite(SInt16 fd, long *count, const void *buffer) {
    assert(*count <= 65536); if (*count > maxWrite) maxWrite = *count;
    if (diskFull) return dskFulErr;
    if (shortWrite && *count) --*count;
    assert(positions[fd] + *count <= 200000);
    memcpy(bodies[fd] + positions[fd], buffer, (size_t)*count); positions[fd] += *count; sizes[fd] = positions[fd];
    return noErr;
}
'''

SUFFIX = r'''
int main(int argc, char **argv) {
    FSSpec source = {0}, target = {1}; UInt32 crc; long i;
    assert(argc == 2);
    for (i = 0; i < 131111; i++) bodies[0][i] = (char)(i % 256);
    memcpy(bodies[1], bodies[0], 131111); sizes[0] = sizes[1] = 131111;
    if (!strcmp(argv[1], "exact")) {
        assert(copy_fork(&source, &target, FALSE, 131111) == noErr);
        assert(copy_fork(&source, &target, TRUE, 131111) == noErr);
        assert(sizes[2] == 131111 && sizes[3] == 131111);
        assert(!memcmp(bodies[0], bodies[2], 131111) && !memcmp(bodies[1], bodies[3], 131111));
        assert(fork_crc(&source, FALSE, &crc, 131111) == noErr && crc == EXPECTED_CRC);
        assert(fork_crc(&source, TRUE, &crc, 131111) == noErr && crc == EXPECTED_CRC);
        assert(maxRead == 65536 && maxWrite == 65536 && yields >= 4);
    } else if (!strcmp(argv[1], "limits")) {
        assert(copy_fork(&source, &target, FALSE, 131110) == paramErr);
        assert(fork_crc(&source, FALSE, &crc, 131110) == paramErr);
    } else if (!strcmp(argv[1], "allocation")) {
        failAllocation = 1;
        assert(copy_fork(&source, &target, FALSE, 131111) == memFullErr);
        assert(fork_crc(&source, FALSE, &crc, 131111) == memFullErr);
    } else {
        shortWrite = 1; assert(copy_fork(&source, &target, FALSE, 131111) == dskFulErr);
        shortWrite = 0; diskFull = 1; assert(copy_fork(&source, &target, FALSE, 131111) == dskFulErr);
        diskFull = 0; readError = 1; assert(copy_fork(&source, &target, FALSE, 131111) == ioErr);
        assert(fork_crc(&source, FALSE, &crc, 131111) == ioErr);
    }
    assert(!allocations && !openFiles); return 0;
}
'''


class ForkIOTests(unittest.TestCase):
    def test_copy_crc_yield_limits_allocation_and_io_failure_cleanup(self):
        compiler = shutil.which("cc")
        if not compiler: self.skipTest("Host C compiler required")
        source = Path(__file__).with_name("RemoteMac.c").read_text()
        crc = zlib.crc32(bytes(range(256)) * 512 + bytes(range(39)))
        code = PREFIX + "\n#define EXPECTED_CRC 0x%08xu\n" % crc
        code += "\n".join(function(source, name) for name in ("file_yield", "copy_fork", "crc_update", "fork_crc")) + SUFFIX
        with tempfile.TemporaryDirectory(prefix="agentchat-fork-io-") as temp:
            c = Path(temp) / "fork.c"; binary = Path(temp) / "fork"; c.write_text(code)
            command = [compiler, "-std=c99", "-Wall", "-Wextra", "-Werror", str(c), "-o", str(binary)]
            if os.environ.get("AGENTCHAT_TEST_SANITIZERS") == "1": command[1:1] = ["-fsanitize=address,undefined", "-g"]
            subprocess.run(command, check=True, capture_output=True)
            for scenario in ("exact", "limits", "allocation", "errors"):
                with self.subTest(scenario=scenario): subprocess.run([str(binary), scenario], check=True, timeout=5)


if __name__ == "__main__": unittest.main()
