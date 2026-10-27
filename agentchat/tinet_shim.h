/*
 * tinet_shim.h — minimal Open Transport TCP/IP address types.
 *
 * Apple's Universal Interfaces CIncludes lack the TCP/IP convenience types
 * (they ship with the Open Transport SDK). Layout/values per Apple,
 * "Networking with Open Transport" (1997) ch. 29:
 *   AF_INET = 2, AF_DNS = 42
 *   struct InetAddress { fAddressType; fPort; fHost; fUnused[8]; }
 *   void OTInitInetAddress(InetAddress*, InetPort, InetHost)   (pascal)
 * OTInitInetAddress is exported by the OT TCP/IP extension at runtime.
 */
#ifndef TINET_SHIM_H
#define TINET_SHIM_H

#include <Types.h>

enum {
    kAF_INET = 2,
    kAF_DNS  = 42
};

typedef UInt16 InetPort;
typedef UInt32 InetHost;   /* network byte order (big-endian value) */

struct InetAddress {
    OTAddressType fAddressType;
    InetPort      fPort;
    InetHost      fHost;
    UInt8         fUnused[8];
};
typedef struct InetAddress InetAddress;

extern pascal void OTInitInetAddress(InetAddress *addr, InetPort port, InetHost host);

/* parse dotted quad "1.2.3.4" -> big-endian network-order value */
static inline UInt32 ip_to_nbo(const char *s)
{
    UInt32 parts[4];
    int idx = 0;
    parts[0] = parts[1] = parts[2] = parts[3] = 0;
    while (*s && idx < 4) {
        if (*s >= '0' && *s <= '9') {
            parts[idx] = parts[idx] * 10u + (UInt32)(*s - '0');
            s++;
        } else if (*s == '.') {
            idx++;
            s++;
        } else {
            break;
        }
    }
    if (idx != 3) return 0;
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3];
}

/* host-order port -> network-order value */
static inline UInt16 port_to_nbo(UInt16 p)
{
    return (UInt16)(((p & 0x00FFu) << 8) | ((p >> 8) & 0x00FFu));
}

#endif /* TINET_SHIM_H */
