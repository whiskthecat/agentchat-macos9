#ifndef REMOTE_APPLICATIONS_H
#define REMOTE_APPLICATIONS_H

#include <Events.h>
#include <Processes.h>

/* Cooperative, private Apple events for applications which explicitly install
 * an AGCT/cmnd handler. This does not make arbitrary applications scriptable.
 * Payloads are ASCII JSON (Unicode text uses JSON \u escapes), without a NUL.
 */
#define REMOTE_APPLICATION_EVENT_CLASS 'AGCT'
#define REMOTE_APPLICATION_EVENT_ID 'cmnd'
#define REMOTE_APPLICATION_COMMAND_MAX 1024L
#define REMOTE_APPLICATION_REPLY_MAX 4096L
#define REMOTE_APPLICATION_TIMEOUT_TICKS 600UL

/* AESend supplies only null/update/activate/os events here. The application
 * must handle these through its normal redraw/activation/idle housekeeping.
 * Do not call WaitNextEvent, read chat transport input, dispatch another Mac
 * RPC, or send inference from this callback. Return TRUE to cancel the wait.
 * Key/mouse events and unrelated Apple events remain for the normal event loop.
 */
typedef Boolean (*RemoteApplicationIdleProc)(const EventRecord *event, void *context);

typedef struct RemoteApplicationReply {
    OSErr error;
    long applicationError;       /* The receiver's complete signed 32-bit errn. */
    unsigned long elapsedTicks;
    long textBytes;
    char text[REMOTE_APPLICATION_REPLY_MAX + 1];
    char detail[192];
} RemoteApplicationReply;

#ifdef __cplusplus
extern "C" {
#endif

Boolean RemoteApplicationsBusy(void);

/* Runs only on the application's event thread. Requires an actual, running
 * non-self APPL process serial number, a non-NULL idle callback, and an output
 * record. Sends once; it does not launch, focus, retry, or resume inference.
 * On noErr, text/textBytes hold a complete bounded reply. The bridge should
 * parse it as JSON before exposing it to an agent. A timeout does not establish
 * whether the receiver executed the command; callers must not retry writes.
 */
OSErr RemoteApplicationsCommand(const ProcessSerialNumber *target,
    const char *command, long commandBytes, RemoteApplicationIdleProc idle,
    void *context, RemoteApplicationReply *result);

#ifdef __cplusplus
}
#endif

#endif
