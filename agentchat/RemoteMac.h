/* Classic Toolbox inspection and file service. No UI or inference ownership. */
#ifndef REMOTE_MAC_H
#define REMOTE_MAC_H
#include <Types.h>
#include "RemoteApplications.h"
typedef struct {
    const char *version, *status, *model, *conversation;
    const char *draftImage;
    long transcriptRows, draftBytes, queueBytes, imageRows;
    short conversationTop, modelRows;
    const char *questionID;
    short questionIndex, questionCustomBytes, questionEditBytes;
    Boolean connected, busy, loading, testBuild;
    Boolean imageUploading;
    Boolean imageReady, modelImages;
    Boolean transcriptBuffered;
    unsigned long transcriptFrames, windowFrames;
} RemoteMacDiagnostics;
/* TRUE means every byte was accepted atomically; FALSE retains the reply. */
typedef Boolean (*RemoteMacSendProc)(const char *, long);
typedef void (*RemoteMacStateProc)(RemoteMacDiagnostics *);
typedef OSErr (*RemoteMacActionProc)(const char *, const char *);
void RemoteMacInit(RemoteMacSendProc, RemoteMacStateProc, RemoteMacActionProc);
void RemoteMacSetApplicationIdle(RemoteApplicationIdleProc, void *context);
Boolean RemoteMacHandle(char *command);
/* Exact-length binary phase. Any error/expiry requires transport disconnect. */
OSErr RemoteMacBinaryBegin(char *header);
long RemoteMacBinaryRemaining(void);
char *RemoteMacBinaryBuffer(void);
OSErr RemoteMacBinaryReceive(const char *bytes, long count);
Boolean RemoteMacBinaryFailed(void);
/* Stop consuming incoming commands while FALSE, including any buffered tail. */
Boolean RemoteMacCanReceive(void);
/* Reserve this capacity before scheduling optional image-upload bytes. */
long RemoteMacPendingReplyBytes(void);
void RemoteMacTick(void);
/* TRUE while remote operations are arriving; the event loop then polls without sleeping. */
Boolean RemoteMacBusy(void);
void RemoteMacEvent(const char *kind, const char *detail);
void RemoteMacReset(void);
#endif
