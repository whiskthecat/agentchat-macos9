#ifndef IDLE_METRICS_H
#define IDLE_METRICS_H
#include <Types.h>
void IdleMetricsBegin(short event, Boolean background, unsigned long sleep);
void IdleMetricsEnd(void);
void IdleMetricsReceive(void);
void IdleMetricsSend(void);
void IdleMetricsNotify(void);
OSErr IdleMetricsJSON(char *output, long capacity);
#endif
