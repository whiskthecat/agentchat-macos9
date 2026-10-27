/* Bounded byte queue primitives: sent prefix is reusable; pending order is exact. */
#ifndef AGENTCHAT_SEND_QUEUE_H
#define AGENTCHAT_SEND_QUEUE_H
#include <string.h>

enum { kSendQueueOK = 0, kSendQueueFull = 1, kSendQueueInvalid = 2 };

static int SendQueueValid(long capacity, long length, long position) {
    return capacity >= 0 && position >= 0 && position <= length && length <= capacity;
}

static int SendQueueCompact(char *bytes, long capacity, long *length, long *position) {
    long pending;
    if (!bytes || !length || !position || !SendQueueValid(capacity, *length, *position))
        return kSendQueueInvalid;
    if (*position) {
        pending = *length - *position;
        if (pending) memmove(bytes, bytes + *position, (size_t)pending);
        *length = pending; *position = 0;
    }
    return kSendQueueOK;
}

static int SendQueueAppend(char *bytes, long capacity, long *length, long *position,
                           const char *source, long count) {
    if (!bytes || !length || !position || count < 0 || (count && !source) ||
        !SendQueueValid(capacity, *length, *position)) return kSendQueueInvalid;
    /* Reject atomically before changing even the queue offsets. Avoid len+count overflow. */
    if (count > capacity - (*length - *position)) return kSendQueueFull;
    if (count > capacity - *length) SendQueueCompact(bytes, capacity, length, position);
    if (count) memcpy(bytes + *length, source, (size_t)count);
    *length += count;
    return kSendQueueOK;
}
#endif
