#ifndef AGENTCHAT_HARDWARE_INFO_H
#define AGENTCHAT_HARDWARE_INFO_H
#include <Types.h>
/* Read-only Toolbox/Gestalt/Name Registry inventory; no firmware writes. */
OSErr HardwareInfoJSON(char *output, long capacity);
OSErr HardwareRegistryProbeJSON(const char *path, const char *property, char *output, long capacity);
#endif
