#ifndef RELAY_DISCOVERY_H
#define RELAY_DISCOVERY_H
#include <Types.h>

typedef struct {
    char host[64];
    UInt16 chatPort, updatePort;
} DiscoveredRelay;

/* InitOpenTransport must have succeeded. Poll from the normal event loop.
 * Start returns false on setup failure; Poll: 0 waiting, 1 found, -1 done.
 * Only VMTEST supplies a unicast fallback (QEMU user-net drops broadcasts). */
Boolean RelayDiscoveryStart(UInt16 port);
short RelayDiscoveryPoll(DiscoveredRelay *relay);
Boolean RelayDiscoveryActive(void);
void RelayDiscoveryCancel(void);
long RelayDiscoveryLastError(void);
#endif
