#ifndef AGENT_CATALOG_INFO_H
#define AGENT_CATALOG_INFO_H
#include <Files.h>
/* Restores recorded catalog values on an existing unlocked file. Preserves
 * both forks and all other catalog fields. Caller must verify later reads. */
OSErr AgentRestoreCatalog(const FSSpec *file, UInt16 flags, UInt32 created, UInt32 modified, CInfoPBRec *after);
#endif
