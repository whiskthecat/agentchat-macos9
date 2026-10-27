/* Cumulative observations, never a periodic task. No allocation in notifier.
 * Microseconds measures event-thread work, not OS/driver interrupt CPU cost. */
#include "IdleMetrics.h"
#include <OSUtils.h>
#include <Timer.h>
#include <Events.h>
#include <stdio.h>
static unsigned long started, wakes, nulls, backgroundWakes, receiveCalls, sendCalls;
static volatile unsigned long notifications;
static unsigned long begin, maxWork, sleepTicks;
static double workUS;
static Boolean active;
static unsigned long now(void) { UnsignedWide t; Microseconds(&t); return t.lo; }
void IdleMetricsBegin(short event, Boolean background, unsigned long sleep) {
    if (!started) started = TickCount();
    begin = now(); active = true; wakes++;
    if (event == nullEvent) nulls++;
    if (background) backgroundWakes++;
    sleepTicks = sleep;
}
void IdleMetricsEnd(void) {
    unsigned long used;
    if (!active) return;
    used = now() - begin; active = false; workUS += used;
    if (used > maxWork) maxWork = used;
}
void IdleMetricsReceive(void) { receiveCalls++; }
void IdleMetricsSend(void) { sendCalls++; }
void IdleMetricsNotify(void) { notifications++; }
OSErr IdleMetricsJSON(char *out, long capacity) {
    int n = snprintf(out, capacity,
        "{\"since_ticks\":%lu,\"sample_ticks\":%lu,\"wakes\":%lu,\"null_events\":%lu,"
        "\"background_wakes\":%lu,\"work_us\":%.0f,\"max_work_us\":%lu,"
        "\"receive_calls\":%lu,\"send_calls\":%lu,\"ot_notifications\":%lu,\"last_sleep_ticks\":%lu,"
        "\"scope\":\"outer event loop; elapsed outside outer WaitNextEvent; nested waits/preemption can be included; no interrupt attribution; subtract snapshots\"}",
        started, TickCount(), wakes, nulls, backgroundWakes, workUS, maxWork,
        receiveCalls, sendCalls, notifications, sleepTicks);
    return n < 0 || n >= capacity ? memFullErr : noErr;
}
