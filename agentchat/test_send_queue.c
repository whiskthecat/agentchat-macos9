/* Host-only boundary tests. No OpenTransport, socket, model, or Macintosh use. */
#include <assert.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include "SendQueue.h"

static void sentPrefixSpace(void) {
    char queue[16] = "0123456789ABCDEF"; long length = 16, position = 10;
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "abcdefghi", 9) == kSendQueueOK);
    assert(length == 15 && position == 0);
    assert(!memcmp(queue, "ABCDEFabcdefghi", 15));
}
static void exactCapacity(void) {
    char queue[16] = "0123456789ABCDEF"; long length = 16, position = 10;
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "abcdefghij", 10) == kSendQueueOK);
    assert(length == 16 && position == 0);
    assert(!memcmp(queue, "ABCDEFabcdefghij", 16));
}
static void fullIsAtomic(void) {
    char queue[16] = "0123456789ABCDEF", before[16]; long length = 16, position = 10;
    memcpy(before, queue, sizeof(queue));
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "abcdefghijk", 11) == kSendQueueFull);
    assert(length == 16 && position == 10 && !memcmp(queue, before, sizeof(queue)));
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "x", LONG_MAX) == kSendQueueFull);
    assert(length == 16 && position == 10 && !memcmp(queue, before, sizeof(queue)));
}
static void noUnnecessaryCompaction(void) {
    char queue[16] = "0123456789ABCDEF"; long length = 10, position = 7;
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "abc", 3) == kSendQueueOK);
    assert(length == 13 && position == 7 && !memcmp(queue + position, "789abc", 6));
}
static void emptyAndInvalid(void) {
    char queue[16] = "0123456789ABCDEF", before[16]; long length = 16, position = 16;
    assert(SendQueueCompact(queue, sizeof(queue), &length, &position) == kSendQueueOK);
    assert(length == 0 && position == 0);
    memcpy(before, queue, sizeof(queue));
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, NULL, 0) == kSendQueueOK);
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, NULL, 1) == kSendQueueInvalid);
    assert(SendQueueAppend(queue, sizeof(queue), &length, &position, "x", -1) == kSendQueueInvalid);
    assert(length == 0 && position == 0 && !memcmp(queue, before, sizeof(queue)));
    position = 1;
    assert(SendQueueCompact(queue, sizeof(queue), &length, &position) == kSendQueueInvalid);
    assert(length == 0 && position == 1 && !memcmp(queue, before, sizeof(queue)));
}
static void thousandsOfPartialWrites(void) {
    char queue[97], expected[30000], actual[30000];
    long length = 0, position = 0, queued = 0, sent = 0, turn;
    for (turn = 0; turn < 2000; turn++) {
        char record[13]; long size = turn % 13 + 1, take, index;
        for (index = 0; index < size; index++) record[index] = (char)('A' + (turn + index) % 26);
        if (SendQueueAppend(queue, sizeof(queue), &length, &position, record, size) == kSendQueueFull) {
            take = length - position;
            memcpy(actual + sent, queue + position, (size_t)take); sent += take; position += take;
            assert(SendQueueCompact(queue, sizeof(queue), &length, &position) == kSendQueueOK);
            assert(SendQueueAppend(queue, sizeof(queue), &length, &position, record, size) == kSendQueueOK);
        }
        memcpy(expected + queued, record, (size_t)size); queued += size;
        take = (turn * 7) % 17; if (take > length - position) take = length - position;
        memcpy(actual + sent, queue + position, (size_t)take); sent += take; position += take;
        if (turn % 3 == 0) assert(SendQueueCompact(queue, sizeof(queue), &length, &position) == kSendQueueOK);
    }
    memcpy(actual + sent, queue + position, (size_t)(length - position)); sent += length - position;
    assert(sent == queued && !memcmp(expected, actual, (size_t)queued));
}
int main(void) {
    sentPrefixSpace(); exactCapacity(); fullIsAtomic(); noUnnecessaryCompaction();
    emptyAndInvalid(); thousandsOfPartialWrites();
    puts("6 send-queue boundary cases passed; 2000 partial-write iterations preserve exact bytes/order");
    return 0;
}
