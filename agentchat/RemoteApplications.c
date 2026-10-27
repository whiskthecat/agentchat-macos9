#include "RemoteApplications.h"
#include <AppleEvents.h>
#include <MacErrors.h>
#include <OSUtils.h>
#include <Quickdraw.h>
#include <stdio.h>
#include <string.h>

static Boolean gActive, gExpired;
static unsigned long gStarted;
static RemoteApplicationIdleProc gIdle;
static void *gIdleContext;

static Boolean ascii_text(const char *text, long bytes)
{
    long i;
    if (!text || bytes <= 0) return FALSE;
    for (i = 0; i < bytes; ++i) {
        unsigned char ch = (unsigned char)text[i];
        if (!(ch == '\t' || ch == '\r' || ch == '\n' || (ch >= 32 && ch <= 126)))
            return FALSE;
    }
    return TRUE;
}

static OSErr fail(RemoteApplicationReply *result, OSErr error, const char *detail)
{
    result->error = error;
    result->textBytes = 0;
    result->text[0] = 0;
    snprintf(result->detail, sizeof result->detail, "%s", detail);
    return error;
}

static OSErr process_info(const ProcessSerialNumber *target, ProcessInfoRec *info, FSSpec *app)
{
    memset(info, 0, sizeof *info);
    memset(app, 0, sizeof *app);
    info->processInfoLength = sizeof *info;
    info->processAppSpec = app;
    return GetProcessInformation(target, info);
}

static Boolean same_application(const ProcessInfoRec *a, const FSSpec *appA,
    const ProcessInfoRec *b, const FSSpec *appB)
{
    return a->processNumber.highLongOfPSN == b->processNumber.highLongOfPSN &&
        a->processNumber.lowLongOfPSN == b->processNumber.lowLongOfPSN &&
        a->processType == b->processType &&
        a->processSignature == b->processSignature &&
        a->processLaunchDate == b->processLaunchDate &&
        appA->vRefNum == appB->vRefNum && appA->parID == appB->parID &&
        appA->name[0] == appB->name[0] &&
        memcmp(appA->name + 1, appB->name + 1, appA->name[0]) == 0;
}

static pascal Boolean application_idle(EventRecord *event, long *sleepTime, RgnHandle *mouseRgn)
{
    /* Tick subtraction remains correct across the 32-bit TickCount rollover.
     * This also bounds the wait if a receiver calls AEResetTimer repeatedly.
     */
    if (sleepTime) *sleepTime = 1;
    if (mouseRgn) *mouseRgn = NULL;
    if (TickCount() - gStarted >= REMOTE_APPLICATION_TIMEOUT_TICKS) {
        gExpired = TRUE;
        return TRUE;
    }
    if (event && gIdle) {
        switch (event->what) {
            case nullEvent:
            case updateEvt:
            case activateEvt:
            case osEvt:
                return gIdle(event, gIdleContext);
            default:
                break;
        }
    }
    return FALSE;
}

static OSErr read_text(const AppleEvent *reply, AEKeyword key, char *text, Size capacity, Size *bytes)
{
    DescType type = typeNull;
    Size size = 0, received = 0;
    OSErr error;
    *bytes = 0;
    text[0] = 0;
    error = AESizeOfParam(reply, key, &type, &size);
    if (error != noErr) return error;
    if (type != typeChar) return errAEWrongDataType;
    if (size < 0 || size >= capacity) return errAEBufferTooSmall;
    error = AEGetParamPtr(reply, key, typeChar, &type, text, size, &received);
    if (error != noErr) return error;
    if (received != size || !ascii_text(text, received)) return errAEWrongDataType;
    text[received] = 0;
    *bytes = received;
    return noErr;
}

Boolean RemoteApplicationsBusy(void)
{
    return gActive;
}

OSErr RemoteApplicationsCommand(const ProcessSerialNumber *target,
    const char *command, long commandBytes, RemoteApplicationIdleProc idle,
    void *context, RemoteApplicationReply *result)
{
    ProcessSerialNumber self;
    ProcessInfoRec before, after;
    FSSpec appBefore, appAfter;
    AEAddressDesc address = { typeNull, NULL };
    AppleEvent event = { typeNull, NULL }, reply = { typeNull, NULL };
    AEIdleUPP idleUPP = NULL;
    GrafPtr savedPort;
    Boolean isSelf = FALSE;
    OSErr error;
    DescType errorType = typeNull;
    Size errorBytes = 0, textBytes = 0;
    long applicationError = 0;
    const char *detail = "Application command failed";

    if (!result) return paramErr;
    memset(result, 0, sizeof *result);
    if (gActive) return fail(result, errAEEventFailed, "Another application command is running");
    if (!target || !idle || commandBytes <= 0 || commandBytes > REMOTE_APPLICATION_COMMAND_MAX ||
        !ascii_text(command, commandBytes))
        return fail(result, paramErr, "Command must be 1..1024 ASCII bytes; an idle callback is required");
    /* Reject special process selectors: the broker must choose an actual PSN. */
    if (target->highLongOfPSN == 0 && target->lowLongOfPSN <= kCurrentProcess)
        return fail(result, paramErr, "A running application's actual process serial number is required");
    error = GetCurrentProcess(&self);
    if (error == noErr) error = SameProcess(target, &self, &isSelf);
    if (error != noErr) return fail(result, error, "Cannot validate the target process");
    if (isSelf) return fail(result, paramErr, "AgentChat cannot send application commands to itself");
    error = process_info(target, &before, &appBefore);
    if (error != noErr) return fail(result, error, "Target application is no longer running");
    if (before.processType != 'APPL') return fail(result, paramErr, "Target process is not an APPL application");
    if (!(before.processMode & modeHighLevelEventAware))
        return fail(result, destPortErr, "Target application does not accept high-level events");

    GetPort(&savedPort);
    gActive = TRUE;
    gExpired = FALSE;
    gStarted = TickCount();
    gIdle = idle;
    gIdleContext = context;

    error = AECreateDesc(typeProcessSerialNumber, target, sizeof *target, &address);
    if (error != noErr) { detail = "Cannot create the target Apple-event address"; goto cleanup; }
    error = AECreateAppleEvent(REMOTE_APPLICATION_EVENT_CLASS, REMOTE_APPLICATION_EVENT_ID,
        &address, kAutoGenerateReturnID, kAnyTransactionID, &event);
    if (error != noErr) { detail = "Cannot create the application command event"; goto cleanup; }
    error = AEPutParamPtr(&event, keyDirectObject, typeChar, command, commandBytes);
    if (error != noErr) { detail = "Cannot attach the application command"; goto cleanup; }
    idleUPP = NewAEIdleUPP(application_idle);
    if (!idleUPP) { error = memFullErr; detail = "Cannot allocate the Apple-event idle UPP"; goto cleanup; }

    /* Classic AESend calls WaitNextEvent itself. No nested input pump or reply
     * filter: unrelated high-level events stay queued until our normal loop.
     * The target is never brought forward or invited to display a dialog.
     * Leave native session reconnection enabled: a closed cached PPC session
     * does not establish that this validated PSN exited. AESend owns that
     * connection handling; this module never resends a command after failure.
     */
    error = AESend(&event, &reply,
        kAEWaitReply | kAENeverInteract | kAEDontRecord,
        kAENormalPriority, REMOTE_APPLICATION_TIMEOUT_TICKS, idleUPP, NULL);
    if (gExpired) error = errAETimeout;
    if (error != noErr) {
        if (error == errAETimeout) detail = "Application reply timed out; command may have executed";
        else if (error == errAEWaitCanceled) detail = "Application wait canceled; command may have executed";
        else if (error == errAEEventNotHandled) detail = "Application has no AGCT/cmnd command handler";
        else if (error == sessClosedErr) detail = "Apple-event session closed; command may have executed; inspect target state before another command";
        else detail = "Could not receive the application's Apple-event reply";
        goto cleanup;
    }
    error = process_info(target, &after, &appAfter);
    if (error != noErr || !same_application(&before, &appBefore, &after, &appAfter)) {
        error = procNotFound;
        detail = "Target application exited or changed identity while handling the command";
        goto cleanup;
    }

    error = AESizeOfParam(&reply, keyErrorNumber, &errorType, &errorBytes);
    if (error != noErr && error != errAEDescNotFound) {
        detail = "Cannot inspect the application's error result";
        goto cleanup;
    }
    if (error == noErr) {
        if (errorType != typeSInt32 || errorBytes != sizeof applicationError) {
            error = errAEWrongDataType;
            detail = "Application returned an invalid error number";
            goto cleanup;
        }
        error = AEGetParamPtr(&reply, keyErrorNumber, typeSInt32, &errorType,
            &applicationError, sizeof applicationError, &errorBytes);
        if (error != noErr || errorBytes != sizeof applicationError) {
            if (error == noErr) error = errAEWrongDataType;
            detail = "Cannot read the application's error number";
            goto cleanup;
        }
        result->applicationError = applicationError;
        if (applicationError != noErr) {
            error = applicationError >= -32768L && applicationError <= 32767L ?
                (OSErr)applicationError : errAEEventFailed;
            /* Error strings are optional. Never allocate or truncate a large
             * receiver payload; fall back to a bounded local error description.
             */
            if (read_text(&reply, keyErrorString, result->detail, sizeof result->detail, &textBytes) == noErr)
                detail = NULL;
            else detail = "Application reported a command error";
            goto cleanup;
        }
    }
    error = read_text(&reply, keyDirectObject, result->text, sizeof result->text, &textBytes);
    if (error != noErr) {
        detail = error == errAEBufferTooSmall ? "Application reply exceeds 4096 bytes" :
            "Application reply must contain nonempty ASCII typeChar text";
        goto cleanup;
    }
    result->textBytes = textBytes;

cleanup:
    result->elapsedTicks = TickCount() - gStarted;
    if (idleUPP) { DisposeAEIdleUPP(idleUPP); }
    AEDisposeDesc(&reply);
    AEDisposeDesc(&event);
    AEDisposeDesc(&address);
    SetPort(savedPort);
    gIdle = NULL;
    gIdleContext = NULL;
    gActive = FALSE;
    result->error = error;
    if (error != noErr) {
        result->textBytes = 0;
        result->text[0] = 0;
        if (detail) snprintf(result->detail, sizeof result->detail, "%s", detail);
    }
    return error;
}
