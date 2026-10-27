/* Model management: modeless Toolbox window, List Manager table and native
 * checkbox tracking. Visibility changes are saved immediately on the bridge. */
#include <Lists.h>
#include <Controls.h>
#include <ControlDefinitions.h>
#include <Appearance.h>
#include <Fonts.h>
#include <Memory.h>
#include <MixedMode.h>
#include <OSUtils.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "ModelSettings.h"
#define CAP 256
#define ROW_H 22
#define CHECK_W 54
#define NAME_W 270

typedef struct { char id[128], name[64], source[64]; Boolean shown, recommended; } Entry;
static Entry entries[CAP], incoming[CAP];
static short count, incomingCount;
static Boolean received, loading, connected;
static short controlState = -1;
static ModelSettingsSendProc sendLine;
static ListHandle list;
static DialogPtr dialog;
static UserItemUPP drawing;
static ListDefUPP rowDrawing;
static Handle definition, originalDefinition;

static void text(short item, const char *s) {
    DialogItemType type; Handle h; Rect r; Str255 p; long n = strlen(s);
    if (n > 255) n = 255; p[0] = n; memcpy(p + 1, s, n);
    GetDialogItem(dialog, item, &type, &h, &r); SetDialogItemText(h, p);
}
static short selected(void) {
    Cell cell = {0, 0};
    return list && LGetSelect(TRUE, &cell, list) && cell.v < count ? cell.v : -1;
}
static void selection(void) {
    short row = selected(); char s[160];
    if (row < 0) { text(6, "Select a model to see its information."); text(7, ""); return; }
    snprintf(s, sizeof s, "Model: %s", entries[row].id); text(6, s);
    snprintf(s, sizeof s, "Provider: %s   Automatic: %s", entries[row].source,
             entries[row].recommended ? "shown" : "hidden"); text(7, s);
}
static void move_item(short item, short x, short y, short w, short h) {
    MoveDialogItem(dialog,item,x,y); SizeDialogItem(dialog,item,w,h);
}
static void layout(void) {
    short w = ((GrafPtr)dialog)->portRect.right, h = ((GrafPtr)dialog)->portRect.bottom;
    short bottom = h - 106; Point size = {ROW_H, w - 48};
    bottom = 72 + ((bottom - 72) / ROW_H) * ROW_H;
    move_item(1,16,72,w-48,bottom-72);
    move_item(2,w-102,h-36,86,20); move_item(3,16,h-36,102,20); move_item(4,128,h-36,86,20);
    move_item(5,16,12,w-32,30); move_item(6,16,h-86,w-32,17);
    move_item(7,16,h-64,w-32,18); move_item(8,16,bottom+4,w-32,16);
    if (list) { LSize(w-48,bottom-72,list); LCellSize(size,list); }
}
static void label(const char *s, Rect r) {
    Str255 p; short n = strlen(s);
    if (n > 255) n = 255;
    while (n > 0 && TextWidth(s, 0, n) > r.right - r.left - 8) n--;
    p[0] = n; memcpy(p + 1, s, n);
    if (n < strlen(s) && n >= 3) p[n] = p[n-1] = p[n-2] = '.';
    MoveTo(r.left + 4, r.top + 15); DrawString(p);
}
static void check_rect(Rect row, Rect *r) {
    SetRect(r, row.left + 20, row.top + 4, row.left + 34, row.top + 18);
}
static void theme_button(Rect *r, short kind, Boolean on) {
#ifndef mc68000
    ThemeButtonDrawInfo info;
    if (kind == kThemeListHeaderButton) { DrawThemePlacard(r,kThemeStateActive); return; }
    info.state = kThemeStateActive; info.value = on ? kThemeButtonOn : kThemeButtonOff;
    info.adornment = kThemeAdornmentNone;
    DrawThemeButton(r, kind, &info, NULL, NULL, NULL, 0);
#else
    FrameRect(r); if (on) { MoveTo(r->left, r->top); LineTo(r->right-1,r->bottom-1); MoveTo(r->right-1,r->top); LineTo(r->left,r->bottom-1); }
#endif
}
static pascal void row_draw(short message, Boolean highlight, Rect *r, Cell cell,
                            short offset, short length, ListHandle l) {
    GrafPtr port; RGBColor fore, back, gray = {0xdddd,0xdddd,0xdddd};
    RGBColor selectedColor = {0xcccc,0xcccc,0xffff};
    PenState pen; RgnHandle clip; Rect column, check; short font, size, mode; Style face;
    if ((message != lDrawMsg && message != lHiliteMsg) || cell.v < 0 || cell.v >= count) return;
    GetPort(&port); SetPort((*l)->port);
    font = (*l)->port->txFont; size = (*l)->port->txSize; face = (*l)->port->txFace; mode = (*l)->port->txMode;
    GetForeColor(&fore); GetBackColor(&back); GetPenState(&pen);
    clip = NewRgn(); if (!clip) { SetPort(port); return; }
    GetClip(clip); ClipRect(r); SectRgn(clip, (*l)->port->clipRgn, (*l)->port->clipRgn);
    PenNormal(); TextFont(kFontIDGeneva); TextSize(10); TextFace(0); TextMode(srcCopy);
    /* Both draw and highlight messages explicitly paint the same row paper.
     * ScrollRect exposes rows without calling the dialog's user-item painter. */
    RGBBackColor(highlight ? &selectedColor : &gray); EraseRect(r); ForeColor(blackColor);
    check_rect(*r, &check); theme_button(&check, kThemeCheckBox, entries[cell.v].shown);
    column = *r; column.left += CHECK_W; column.right = column.left + NAME_W;
    label(entries[cell.v].name, column);
    column.left = column.right; column.right = r->right;
    label(entries[cell.v].source, column);
    ForeColor(whiteColor); MoveTo(r->left, r->bottom - 1); LineTo(r->right - 1, r->bottom - 1);
    MoveTo(r->left + CHECK_W - 1, r->top); LineTo(r->left + CHECK_W - 1, r->bottom - 1);
    MoveTo(r->left + CHECK_W + NAME_W - 1, r->top); LineTo(r->left + CHECK_W + NAME_W - 1, r->bottom - 1);
    SetClip(clip); DisposeRgn(clip); SetPenState(&pen); RGBForeColor(&fore); RGBBackColor(&back);
    TextFont(font); TextSize(size); TextFace(face); TextMode(mode); SetPort(port);
}
static int order(const void *a, const void *b) {
    const Entry *x = a, *y = b;
    if (x->shown != y->shown) return x->shown ? -1 : 1;
    return strcmp(x->name, y->name);
}
static void populate(void) {
    GrafPtr saved; short i, row = selected(); char id[128] = ""; Cell cell = {0,0};
    if (row >= 0) strcpy(id, entries[row].id);
    memcpy(entries, incoming, incomingCount * sizeof(Entry)); count = incomingCount;
    qsort(entries, count, sizeof(Entry), order);
    GetPort(&saved); SetPort((GrafPtr)dialog);
    LSetDrawingMode(FALSE, list); LDelRow(0, 0, list);
    if (count) LAddRow(count, 0, list);
    /* Cell data is just an index; long provider IDs cannot hit the 32K List
     * Manager cell-data limit. Entries retain stable IDs across discovery. */
    for (i = 0; i < count; i++) { cell.v = i; LSetCell(&i, sizeof i, cell, list); if (!strcmp(entries[i].id,id)) row = i; }
    cell.v = row >= 0 && row < count ? row : 0;
    if (count) LSetSelect(TRUE, cell, list);
    LSetDrawingMode(TRUE, list); InvalRect(&((GrafPtr)dialog)->portRect);
    text(8, count ? "Changes are saved immediately. Hiding a model keeps its open chat." : "No models available. Connect a provider on the bridge.");
    selection(); SetPort(saved);
}
static pascal void draw_list(DialogPtr d, short item) {
    GrafPtr saved; RGBColor fore, back, gray = {0xdddd,0xdddd,0xdddd};
    DialogItemType type; Handle h; Rect r, header; short col;
    static const char *titles[] = {"On/Off", "Name", "Provider"};
    GetPort(&saved); SetPort((GrafPtr)d); GetForeColor(&fore); GetBackColor(&back);
    TextFont(kFontIDGeneva); TextSize(10); TextFace(0); TextMode(srcCopy); PenNormal();
    GetDialogItem(d, item, &type, &h, &r);
    RGBBackColor(&gray); ForeColor(blackColor); EraseRect(&r);
    if (list) LUpdate(((GrafPtr)d)->visRgn, list);
    header = r; header.top -= 20; header.bottom = r.top; header.right = header.left + CHECK_W;
    for (col = 0; col < 3; col++) {
        theme_button(&header, kThemeListHeaderButton, FALSE); ForeColor(blackColor); label(titles[col],header);
        header.left = header.right; header.right = col == 0 ? header.left + NAME_W : r.right;
    }
    header.left = r.right; header.right = r.right + 16;
    theme_button(&header, kThemeListHeaderButton, FALSE);
    r.top -= 20; r.right += 16; InsetRect(&r, -1, -1); ForeColor(blackColor); FrameRect(&r);
    RGBForeColor(&fore); RGBBackColor(&back); SetPort(saved);
}
void ModelSettingsInit(ModelSettingsSendProc send) { sendLine = send; }
void ModelSettingsLine(char *p) {
    char *fields[5]; short i; Entry *entry;
    if (!dialog) return;
    if (!strcmp(p, "BEGIN")) { incomingCount = 0; return; }
    if (!strcmp(p, "END")) { received = TRUE; return; }
    if (strncmp(p, "ITEM\t", 5) || incomingCount >= CAP) return;
    fields[0] = p + 5;
    for (i = 1; i < 5; i++) { char *tab = strchr(fields[i-1], '\t'); if (!tab) return; *tab = 0; fields[i] = tab + 1; }
    if (strlen(fields[0]) >= sizeof entries[0].id) return;
    entry = &incoming[incomingCount++]; memset(entry, 0, sizeof *entry);
    strcpy(entry->id, fields[0]); strncpy(entry->name, fields[1], 63); strncpy(entry->source, fields[2], 63);
    entry->shown = !strcmp(fields[3], "1"); entry->recommended = !strcmp(fields[4], "1");
}
Boolean ModelSettingsIsOpen(void) { return dialog != NULL; }
void ModelSettingsTick(Boolean online) {
    GrafPtr saved; DialogItemType type; Handle h; Rect r; short i;
    if (!dialog) return;
    GetPort(&saved); SetPort((GrafPtr)dialog);
    if (connected && !online) { loading = FALSE; text(8, "Disconnected. Reconnect to change model visibility."); }
    if (!connected && online) { loading = TRUE; text(8, "Loading connected providers..."); sendLine("W:LIST"); }
    connected = online;
    if (received) { received = loading = FALSE; populate(); }
    if (controlState != (online && !loading)) {
        controlState = online && !loading;
        for (i = 3; i <= 4; i++) { GetDialogItem(dialog,i,&type,&h,&r); HiliteControl((ControlHandle)h, controlState ? 0 : 255); }
    }
    SetPort(saved);
}
void ModelSettingsClose(void) {
    GrafPtr current;
    if (!dialog) return;
    GetPort(&current);
    /* The default LDEF is a shared Resource Manager handle; only our private
     * descriptor handle and UPP belong to this window. */
    if (list) { if (originalDefinition) (*list)->listDefProc = originalDefinition; LDispose(list); list = NULL; }
    originalDefinition = NULL;
    if (definition) DisposeHandle(definition); definition = NULL;
    if (rowDrawing) DisposeListDefUPP(rowDrawing); rowDrawing = NULL;
    DisposeDialog(dialog);
    if (current == (GrafPtr)dialog && FrontWindow()) SetPort(FrontWindow());
    dialog = NULL;
    if (drawing) DisposeUserItemUPP(drawing); drawing = NULL;
}
void ModelSettingsOpen(void) {
    Rect view, bounds; Point size = {ROW_H,0}; DialogItemType type; Handle h; GrafPtr saved;
    if (dialog) { if (IsWindowCollapsed((WindowPtr)dialog)) CollapseWindow((WindowPtr)dialog,FALSE); ShowWindow((WindowPtr)dialog); SelectWindow((WindowPtr)dialog); return; }
    if (!sendLine) return;
    GetPort(&saved); dialog = GetNewDialog(137, NULL, (WindowPtr)-1); if (!dialog) return;
    SetPort((GrafPtr)dialog); count = incomingCount = 0; received = FALSE; loading = connected = TRUE; controlState = -1;
    GetDialogItem(dialog, 1, &type, &h, &view);
    SetRect(&bounds, 0, 0, 1, 0); size.h = view.right - view.left;
    list = LNew(&view, &bounds, size, 0, (WindowPtr)dialog, FALSE, FALSE, FALSE, TRUE);
    if (list) originalDefinition = (*list)->listDefProc;
    rowDrawing = NewListDefUPP(row_draw);
    /* Classic List Manager expects a handle to executable LDEF code. PPC uses
     * a Mixed Mode routine descriptor; 68K uses a JMP to its Pascal callback. */
#ifdef mc68000
    definition = NewHandle(6);
    if (definition && rowDrawing) { UInt32 address = (UInt32)rowDrawing; (*definition)[0] = 0x4e; (*definition)[1] = 0xf9; BlockMoveData(&address, *definition + 2, 4); }
#else
    definition = NewHandle(sizeof(RoutineDescriptor));
    if (definition && rowDrawing) BlockMoveData((Ptr)rowDrawing, *definition, sizeof(RoutineDescriptor));
#endif
    if (!list || !rowDrawing || !definition) { ModelSettingsClose(); SetPort(saved); return; }
    (*list)->listDefProc = definition;
    (*list)->selFlags = lOnlyOne; LSetDrawingMode(TRUE, list);
    drawing = NewUserItemUPP(draw_list); SetDialogItem(dialog, 1, userItem, (Handle)drawing, &view);
    layout();
    SetDialogDefaultItem(dialog, 2); ShowWindow((WindowPtr)dialog); SelectWindow((WindowPtr)dialog);
    selection(); sendLine("W:LIST"); SetPort(saved);
}
static void toggle(short row) {
    char command[160]; Cell cell = {row,0};
    if (!connected || loading || row < 0 || row >= count) return;
    entries[row].shown = !entries[row].shown;
    snprintf(command, sizeof command, "W:APPLY\t0\t%s\t%d", entries[row].id, entries[row].shown);
    sendLine(command); LDraw(cell, list); selection();
}
Boolean ModelSettingsEvent(EventRecord *e) {
    WindowPtr w = NULL; GrafPtr saved; short part, hit, row; Point p;
    DialogPtr selectedDialog;
    if (!dialog) return FALSE;
    /* IsDialogEvent services native hdlg/HelpItem balloon tracking even when
     * our table handles its own mouse events. Modeless dialogs need idle calls. */
    if (e->what == nullEvent && FrontWindow() == (WindowPtr)dialog) {
        GetPort(&saved); SetPort((GrafPtr)dialog);
        if (IsDialogEvent(e)) DialogSelect(e,&selectedDialog,&hit);
        SetPort(saved);
    }
    if ((e->what == keyDown || e->what == autoKey) && FrontWindow() == (WindowPtr)dialog) {
        short ch = e->message & charCodeMask;
        if (((e->modifiers & cmdKey) && (ch == 'w' || ch == 'W')) || ch == 27 || ch == '\r') { ModelSettingsClose(); return TRUE; }
        if (!(e->modifiers & cmdKey)) {
            GetPort(&saved); SetPort((GrafPtr)dialog); row = selected();
            if (ch == ' ') toggle(row);
            else if (ch == 30 || ch == 31) {
                Cell old = {row,0}, next;
                if (row >= 0) LSetSelect(FALSE,old,list);
                row += ch == 30 ? -1 : 1; if (row < 0) row = 0; if (row >= count) row = count-1;
                next.h = 0; next.v = row;
                if (count) { LSetSelect(TRUE,next,list); LAutoScroll(list); } selection();
            }
            SetPort(saved); return TRUE;
        }
    }
    if (e->what == updateEvt && (WindowPtr)e->message == (WindowPtr)dialog) {
        Rect grow = ((GrafPtr)dialog)->portRect; RgnHandle clip = NewRgn();
        GetPort(&saved); SetPort((GrafPtr)dialog); BeginUpdate((WindowPtr)dialog); DrawDialog(dialog);
        if (clip) { GetClip(clip); grow.left = grow.right-15; grow.top = grow.bottom-15; ClipRect(&grow); DrawGrowIcon((WindowPtr)dialog); SetClip(clip); DisposeRgn(clip); }
        EndUpdate((WindowPtr)dialog); SetPort(saved); return TRUE;
    }
    if (e->what == activateEvt && (WindowPtr)e->message == (WindowPtr)dialog) {
        GetPort(&saved); SetPort((GrafPtr)dialog); LActivate(TRUE,list); SetPort(saved); return TRUE;
    }
    if (e->what != mouseDown) return FALSE;
    part = FindWindow(e->where,&w);
    /* FindWindow need not assign a window for menu-bar/desktop hits. */
    if (part == inMenuBar || w != (WindowPtr)dialog) return FALSE;
    if (part == inDrag) { Rect limit = qd.screenBits.bounds; DragWindow(w,e->where,&limit); return TRUE; }
    if (part == inGoAway) { if (TrackGoAway(w,e->where)) ModelSettingsClose(); return TRUE; }
    if (part == inCollapseBox) { if (TrackBox(w,e->where,part)) CollapseWindow(w,!IsWindowCollapsed(w)); return TRUE; }
    if (part == inGrow || part == inZoomIn || part == inZoomOut) {
        GetPort(&saved); SetPort(w);
        if (part == inGrow) {
            Rect limits = {420,544,560,790}; long size = GrowWindow(w,e->where,&limits);
            if (size) { SizeWindow(w,size & 0xffff,(unsigned long)size >> 16,TRUE); layout(); InvalRect(&w->portRect); }
        } else if (TrackBox(w,e->where,part)) { ZoomWindow(w,part,TRUE); layout(); InvalRect(&w->portRect); }
        SetPort(saved); return TRUE;
    }
    if (part != inContent) return TRUE;
    SelectWindow(w); GetPort(&saved); SetPort(w); p = e->where; GlobalToLocal(&p);
    if (PtInRect(p,&(*list)->rView)) {
        row = (*list)->visible.top + (p.v - (*list)->rView.top) / ROW_H;
        if (row < count && p.h < (*list)->rView.left + CHECK_W && connected && !loading) {
            Rect r, check; ControlHandle control; Cell old = {0,0}, cell = {row,0};
            if (LGetSelect(TRUE,&old,list)) LSetSelect(FALSE,old,list);
            LSetSelect(TRUE,cell,list); LRect(&r,cell,list); check_rect(r,&check);
            control = NewControl(w,&check,"\p",TRUE,entries[row].shown,0,1,checkBoxProc,0);
            if (control) { hit = TrackControl(control,p,NULL); DisposeControl(control); if (hit) toggle(row); else LDraw(cell,list); }
            selection();
        } else { LClick(p,FALSE,list); selection(); }
        SetPort(saved); return TRUE;
    }
    { Rect r = (*list)->rView; r.left = r.right; r.right += 16;
      if (PtInRect(p,&r)) { LClick(p,FALSE,list); SetPort(saved); return TRUE; } }
    if (DialogSelect(e,&selectedDialog,&hit)) {
        if (hit == 2) { SetPort(saved); ModelSettingsClose(); return TRUE; }
        if (hit == 3 && connected && !loading) {
            short i; sendLine("W:APPLY\t1");
            for (i=0;i<count;i++) entries[i].shown = entries[i].recommended;
            memcpy(incoming,entries,count*sizeof(Entry)); incomingCount = count; populate();
        }
        if (hit == 4 && connected && !loading) { loading = TRUE; text(8,"Refreshing connected providers..."); sendLine("W:LIST"); }
    }
    SetPort(saved); return TRUE;
}
