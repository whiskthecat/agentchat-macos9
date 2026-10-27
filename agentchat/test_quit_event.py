#!/usr/bin/env python3
"""Exercise production Apple Event quit functions with recording Toolbox fakes.

No endpoint, Macintosh, bridge, prompt or tool-question reply is used. Native
PPC/68K builds separately validate the actual Apple Event Manager ABI/imports.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parent


def function(source, name):
    match = re.search(r"^static (?:pascal )?(?:void|Boolean|OSErr) " + re.escape(name)
                      + r"\([^;]*?\)\s*\{", source, re.M)
    if not match:
        raise ValueError("Missing production function " + name)
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[match.start():end] + "\n"


PREFIX = r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#define pascal
#define TRUE 1
#define FALSE 0
#define noErr 0
#define userCanceledErr -128
#define memFullErr -108
#define kCoreEventClass 'aevt'
#define kAEQuitApplication 'quit'
typedef unsigned char Boolean;
typedef short OSErr;
typedef struct { unsigned char bytes[32]; } AppleEvent;
typedef OSErr (*AEEventHandlerUPP)(const AppleEvent*, AppleEvent*, long);
typedef void *ModalFilterUPP;
typedef struct { short teLength; char text[2049]; } TestTE;
typedef TestTE **TEHandle;
static TestTE textRecord, *textPointer = &textRecord;
static TEHandle gTE = &textPointer;
static Boolean gBusy, gHistoryLoading, imageUploading, questionPending, questionOpen, settingsOpen;
static char gDraftImage[72], gNewImage[72], gNewDraft[2049];
static AEEventHandlerUPP gQuitEventUPP;
static Boolean gDeferredQuit;
static short gModalDepth;
static void *gTranscriptWorld;
static int allocations, installs, removals, disposals, exits, cleanupCalls;
static int allocationFailure, installError, inAppleEvent, replySent, requireReply;
static OSErr modalResult;
static Boolean ChatImagesUploading(void) { return imageUploading; }
static Boolean ToolQuestionsPending(void) { return questionPending; }
static Boolean ToolQuestionsIsOpen(void) { return questionOpen; }
static Boolean ModelSettingsIsOpen(void) { return settingsOpen; }
static AEEventHandlerUPP NewAEEventHandlerUPP(AEEventHandlerUPP handler) {
    allocations++; return allocationFailure ? NULL : handler;
}
static OSErr AEInstallEventHandler(unsigned long cls, unsigned long id, AEEventHandlerUPP handler, long refcon, Boolean system) {
    assert(cls == kCoreEventClass && id == kAEQuitApplication && handler && !refcon && !system);
    installs++; return (OSErr)installError;
}
static OSErr AERemoveEventHandler(unsigned long cls, unsigned long id, AEEventHandlerUPP handler, Boolean system) {
    assert(cls == kCoreEventClass && id == kAEQuitApplication && handler && !system && !inAppleEvent);
    removals++; return noErr;
}
static void DisposeAEEventHandlerUPP(AEEventHandlerUPP handler) { assert(handler); disposals++; }
static void ModelSettingsClose(void) { assert(!inAppleEvent); cleanupCalls++; }
static void ChatImagesClose(void) { assert(!inAppleEvent); cleanupCalls++; }
static void RemoteMacReset(void) { assert(!inAppleEvent); cleanupCalls++; }
static void DisposeGWorld(void *world) { assert(world && !inAppleEvent); cleanupCalls++; }
static void ExitToShell(void) { assert(!inAppleEvent && (!requireReply || replySent)); exits++; }
static void ModalDialog(ModalFilterUPP filter, short *hit);
'''

SUFFIX = r'''
typedef struct {
    ModelRecord models[MODEL_CACHE]; ChatRecord chats[HISTORY_CACHE];
    TestTE te; char draftImage[72], newImage[72], newDraft[2049];
    Boolean busy, history, upload, pending, question, settings; short modal;
} Preserved;
static Preserved before, after;
static void snapshot(Preserved *p) {
    memset(p, 0, sizeof(*p));
    memcpy(p->models, gModels, sizeof(gModels)); memcpy(p->chats, gChats, sizeof(gChats));
    p->te = textRecord; memcpy(p->draftImage, gDraftImage, sizeof(gDraftImage));
    memcpy(p->newImage, gNewImage, sizeof(gNewImage)); memcpy(p->newDraft, gNewDraft, sizeof(gNewDraft));
    p->busy = gBusy; p->history = gHistoryLoading; p->upload = imageUploading; p->pending = questionPending;
    p->question = questionOpen; p->settings = settingsOpen; p->modal = gModalDepth;
}
static OSErr deliver(void) {
    AppleEvent event, reply, savedEvent, savedReply;
    OSErr result;
    memset(&event, 37, sizeof(event)); memset(&reply, 93, sizeof(reply));
    savedEvent = event; savedReply = reply;
    inAppleEvent = 1;
    result = handle_quit_event(&event, &reply, 0);
    assert(exits == 0 && cleanupCalls == 0);
    assert(!memcmp(&event, &savedEvent, sizeof(event)) && !memcmp(&reply, &savedReply, sizeof(reply)));
    replySent = 1; inAppleEvent = 0;
    return result;
}
static void preservedRefusal(void) {
    snapshot(&before); assert(!automatic_quit_allowed());
    assert(deliver() == userCanceledErr && !gDeferredQuit);
    finish_deferred_quit(); snapshot(&after);
    assert(!memcmp(&before, &after, sizeof(before)) && !cleanupCalls && !exits);
}
static void ModalDialog(ModalFilterUPP filter, short *hit) {
    assert(filter == NULL && gModalDepth > 0);
    modalResult = deliver(); *hit = 3;
}
static void deferred(void) {
    assert(install_quit_event() == noErr && allocations == 1 && installs == 1);
    requireReply = 1; gTranscriptWorld = &textRecord;
    assert(deliver() == noErr && gDeferredQuit && !exits);
    assert(deliver() == noErr && gDeferredQuit && !exits);
    finish_deferred_quit();
    assert(exits == 1 && cleanupCalls == 4 && removals == 1 && disposals == 1 && !gQuitEventUPP && !gDeferredQuit);
    finish_deferred_quit(); assert(exits == 1);
}
static void refusals(void) {
    gBusy = TRUE; preservedRefusal(); gBusy = FALSE;
    gHistoryLoading = TRUE; preservedRefusal(); gHistoryLoading = FALSE;
    imageUploading = TRUE; preservedRefusal(); imageUploading = FALSE;
    questionPending = TRUE; preservedRefusal(); questionPending = FALSE;
    questionOpen = TRUE; preservedRefusal(); questionOpen = FALSE;
    settingsOpen = TRUE; preservedRefusal(); settingsOpen = FALSE;
    gModalDepth = 1; preservedRefusal(); gModalDepth = 0;
    strcpy(textRecord.text, "unsent question"); textRecord.teLength = 15; preservedRefusal();
    textRecord.teLength = 1; textRecord.text[0] = ' '; textRecord.text[1] = 0; preservedRefusal(); textRecord.teLength = 0;
    strcpy(gDraftImage, "active-picture"); preservedRefusal(); gDraftImage[0] = 0;
    strcpy(gNewDraft, "cached new conversation"); preservedRefusal(); gNewDraft[0] = 0;
    strcpy(gNewImage, "new-conversation-picture"); preservedRefusal(); gNewImage[0] = 0;
    assert(deliver() == noErr && gDeferredQuit && !exits);
}
static void caches(void) {
    int i;
    for (i = 0; i < MODEL_CACHE; i++) {
        strcpy(gModels[i].draft, "retained model draft"); preservedRefusal(); gModels[i].draft[0] = 0;
        strcpy(gModels[i].image, "model-picture"); preservedRefusal(); gModels[i].image[0] = 0;
    }
    for (i = 0; i < HISTORY_CACHE; i++) {
        strcpy(gChats[i].draft, "retained conversation draft"); preservedRefusal(); gChats[i].draft[0] = 0;
        strcpy(gChats[i].image, "conversation-picture"); preservedRefusal(); gChats[i].image[0] = 0;
    }
    assert(automatic_quit_allowed() && !gDeferredQuit && !exits);
}
static void recheck(void) {
    assert(deliver() == noErr); strcpy(gNewDraft, "edit after nested event");
    snapshot(&before); finish_deferred_quit(); snapshot(&after);
    assert(!gDeferredQuit && !exits && !cleanupCalls && !memcmp(&before, &after, sizeof(before)));
    gNewDraft[0] = 0; assert(deliver() == noErr); gBusy = TRUE;
    finish_deferred_quit(); assert(!gDeferredQuit && gBusy && !exits && !cleanupCalls);
}
static void modal(void) {
    short hit = 0;
    strcpy(gNewDraft, "unrelated preserved draft"); run_modal_dialog(NULL, &hit);
    assert(hit == 3 && modalResult == userCanceledErr && !gModalDepth && !gDeferredQuit && !strcmp(gNewDraft, "unrelated preserved draft"));
    gNewDraft[0] = 0; gModalDepth = 1; run_modal_dialog(NULL, &hit);
    assert(gModalDepth == 1 && modalResult == userCanceledErr && !gDeferredQuit && !exits);
}
static void registration(void) {
    allocationFailure = 1; assert(install_quit_event() == memFullErr);
    assert(!gQuitEventUPP && allocations == 1 && !installs && !disposals);
    allocationFailure = 0; installError = -1708; assert(install_quit_event() == -1708);
    assert(!gQuitEventUPP && installs == 1 && disposals == 1 && !removals);
    remove_quit_event(); assert(!removals && disposals == 1);
    installError = 0; assert(install_quit_event() == noErr);
    remove_quit_event(); remove_quit_event();
    assert(!gQuitEventUPP && removals == 1 && disposals == 2 && !exits && !cleanupCalls);
}
static void manual(void) {
    assert(install_quit_event() == noErr); gBusy = TRUE; strcpy(gNewDraft, "manual Quit keeps existing policy");
    quit_app(); assert(exits == 1 && cleanupCalls == 3 && gBusy && !strcmp(gNewDraft, "manual Quit keeps existing policy"));
}
int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "deferred")) deferred();
    else if (!strcmp(argv[1], "refusals")) refusals();
    else if (!strcmp(argv[1], "caches")) caches();
    else if (!strcmp(argv[1], "recheck")) recheck();
    else if (!strcmp(argv[1], "modal")) modal();
    else if (!strcmp(argv[1], "registration")) registration();
    else if (!strcmp(argv[1], "manual")) manual();
    else assert(0);
    printf("PASS %s; inference_requests=0\n", argv[1]); return 0;
}
'''


class QuitEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="agentchat-quit-recording-")
        temp = Path(cls.scratch.name)
        compiler = shutil.which("cc")
        if not compiler:
            raise RuntimeError("A host C compiler is required")
        cls.app = (SOURCE / "agentchat.c").read_text()
        declarations = []
        for name in ("DRAFT_LIMIT", "MODEL_CACHE", "HISTORY_CACHE"):
            declarations.append(re.search(r"^#define " + name + r" .*", cls.app, re.M)[0])
        for name, array in (("ModelRecord", "gModels"), ("ChatRecord", "gChats")):
            declarations.append(re.search(r"^typedef struct \{[^\n]*\} " + name + r";", cls.app, re.M)[0])
            declarations.append(re.search(r"^static " + name + " " + array + r"[^;]*;", cls.app, re.M)[0])
        names = ["automatic_quit_allowed", "handle_quit_event", "install_quit_event", "remove_quit_event",
                 "run_modal_dialog", "quit_app", "finish_deferred_quit"]
        c = PREFIX + "\n".join(declarations) + "\n" + "\n".join(function(cls.app, n) for n in names) + SUFFIX
        (temp / "quit.c").write_text(c)
        cls.binary = temp / "quit"
        command = [compiler, "-std=c99", "-Wall", "-Wextra", "-Werror", "-Wno-multichar",
                   "-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-pie", "-no-pie", "-g",
                   str(temp / "quit.c"), "-o", str(cls.binary)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Recording fake compile failed:\n" + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def scenario(self, name):
        result = subprocess.run([str(self.binary), name], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, AGENT_TEST_MODE="1", UBSAN_OPTIONS="halt_on_error=1"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reply_precedes_cleanup(self): self.scenario("deferred")
    def test_busy_editing_and_drafts_are_preserved(self): self.scenario("refusals")
    def test_every_cached_text_and_image_draft(self): self.scenario("caches")
    def test_recheck_cancels_new_activity_without_data_loss(self): self.scenario("recheck")
    def test_modal_depth_and_existing_cancel(self): self.scenario("modal")
    def test_registration_failure_and_upp_lifetime(self): self.scenario("registration")
    def test_manual_quit_policy_is_unchanged(self): self.scenario("manual")

    def test_outer_loop_and_modal_dispatch(self):
        loop = self.app[self.app.index("int main(void)"):]
        self.assertRegex(loop, r"while \(1\) \{\s*IdleMetricsEnd\(\);\s*finish_deferred_quit\(\);")
        self.assertLess(loop.index("finish_deferred_quit();"), loop.index("WaitNextEvent(everyEvent"))
        self.assertIn("case kHighLevelEvent:\n                AEProcessAppleEvent(&ev);", loop)
        self.assertIn("quitEventError = install_quit_event();", loop)
        self.assertIn("native Quit event unavailable (%d); use File > Quit", loop)
        filter_body = self.app[self.app.index("static pascal Boolean update_filter("):self.app.index("static void cleanup_prev(")]
        self.assertRegex(filter_body, r"ev->what == kHighLevelEvent\) \{\s*AEProcessAppleEvent\(ev\);\s*\*itemHit = 0;\s*return TRUE;")
        self.assertEqual(len(re.findall(r"\bModalDialog\(", self.app)), 1)
        self.assertEqual(len(re.findall(r"\brun_modal_dialog\(", self.app)), 6)
        self.assertRegex(self.app, r"gModalDepth\+\+;\s*StandardPutFile\([^;]*;\s*gModalDepth--;")


if __name__ == "__main__":
    unittest.main(verbosity=2)
