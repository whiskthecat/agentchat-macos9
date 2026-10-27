#include "CatalogInfo.h"
#include <string.h>
static OSErr catalog(const FSSpec *file, CInfoPBRec *pb) {
    memset(pb, 0, sizeof *pb);
    pb->hFileInfo.ioNamePtr = (StringPtr)file->name;
    pb->hFileInfo.ioVRefNum = file->vRefNum;
    pb->hFileInfo.ioDirID = file->parID;
    return PBGetCatInfoSync(pb);
}
OSErr AgentRestoreCatalog(const FSSpec *file, UInt16 flags, UInt32 created, UInt32 modified, CInfoPBRec *after) {
    CInfoPBRec pb; OSErr err = catalog(file, &pb);
    if (err) return err;
    if (pb.hFileInfo.ioFlAttrib & 17) return permErr;
    pb.hFileInfo.ioFlFndrInfo.fdFlags = flags;
    pb.hFileInfo.ioFlCrDat = created;
    pb.hFileInfo.ioFlMdDat = modified;
    pb.hFileInfo.ioDirID = file->parID;
    err = PBSetCatInfoSync(&pb);
    if (!err) err = FlushVol(NULL, file->vRefNum);
    if (!err) err = catalog(file, after);
    return err;
}
