/* Native PICT scrap -> QuickDraw XRGB -> relay PNG. Preview pixels are bounded
 * and cached; older pictures reload through read-only G:FETCH control traffic.
 * Uploading or setting a draft never emits a P: inference request.
 */
#include <QDOffscreen.h>
#include <Scrap.h>
#include <Memory.h>
#include <OSUtils.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "ChatImages.h"
#define SLOTS 512
#define RESIDENT 8
#define CHUNK 1024
static ChatImageSendProc gSend;
static ChatImageNoticeProc gNotice;
static struct { char id[72]; GWorldPtr world; short width,height; Boolean ready,asked; unsigned long used; } gImages[SLOTS];
static GWorldPtr gUpload, gIncoming;
static char gUploadID[72], gIncomingID[72];
static long gPosition, gIncomingPosition;
static Boolean gStarted, gEnded;
static unsigned long gSerial, gClock;
static Handle gSavedScrap[5];
static Boolean gScrapSaved;
static const ResType kScrapTypes[5]={'TEXT','PICT','styl','PNGf','JPEG'};
static const char k64[]="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static void encode(const unsigned char *p,long n,char *out) {
    long i,o=0;
    for(i=0;i<n;i+=3) { unsigned long v=(unsigned long)p[i]<<16;
        if(i+1<n)v|=(unsigned long)p[i+1]<<8; if(i+2<n)v|=p[i+2];
        out[o++]=k64[(v>>18)&63];out[o++]=k64[(v>>12)&63];
        out[o++]=i+1<n?k64[(v>>6)&63]:'=';out[o++]=i+2<n?k64[v&63]:'=';
    } out[o]=0;
}
static long decode(const char *p,unsigned char *out,long cap) {
    long n=strlen(p),i,o=0; if(n%4)return -1;
    for(i=0;i<n;i+=4) { unsigned long v=0;short j,pad=0;
        for(j=0;j<4;j++) { const char *at;
            if(p[i+j]=='=') { if(j<2||i+4!=n)return -1;pad++;v<<=6; }
            else { if(pad||!(at=strchr(k64,p[i+j])))return -1;v=(v<<6)|(at-k64); }
        }
        if(o+3-pad>cap)return -1;out[o++]=(v>>16)&255;
        if(pad<2)out[o++]=(v>>8)&255;if(!pad)out[o++]=v&255;
    } return o;
}
static Boolean valid_id(const char *id) {
    short i; if(strncmp(id,"img_",4)||strlen(id)>=72)return FALSE;
    for(i=4;id[i];i++)if(!((id[i]>='0'&&id[i]<='9')||(id[i]>='a'&&id[i]<='z')||
        (id[i]>='A'&&id[i]<='Z')||id[i]=='_'))return FALSE;
    return i>4;
}
static short slot(const char *id,Boolean create) {
    short i,empty=-1,oldest=0;
    for(i=0;i<SLOTS;i++) { if(!strcmp(gImages[i].id,id))return i;
        if(!gImages[i].id[0])empty=i;
        if(gImages[i].used<gImages[oldest].used)oldest=i;
    }
    if(!create)return -1;i=empty>=0?empty:oldest;
    if(gImages[i].world)DisposeGWorld(gImages[i].world);
    memset(&gImages[i],0,sizeof gImages[i]);strcpy(gImages[i].id,id);return i;
}
static void trim(short keep) {
    short i,n=0,oldest=-1;
    for(i=0;i<SLOTS;i++)if(gImages[i].world) { n++;
        if(i!=keep&&(oldest<0||gImages[i].used<gImages[oldest].used))oldest=i;
    }
    if(n>RESIDENT&&oldest>=0) { DisposeGWorld(gImages[oldest].world);gImages[oldest].world=NULL; }
}
static Rect fit(short width,short height,short maxW,short maxH) {
    Rect r={0,0,height,width};
    if(r.right>maxW) { r.bottom=(long)r.bottom*maxW/r.right;r.right=maxW; }
    if(r.bottom>maxH) { r.right=(long)r.right*maxH/r.bottom;r.bottom=maxH; }
    if(r.right<1)r.right=1;if(r.bottom<1)r.bottom=1;return r;
}
static OSErr world(GWorldPtr *w,const Rect *r) { return NewGWorld(w,32,r,NULL,NULL,0); }
/* QuickDraw may pad rowBytes. The wire uses tightly packed XRGB rows. */
static void pixels(PixMapHandle pm,long at,unsigned char *bytes,long n,Boolean intoWorld) {
    long width=(**pm).bounds.right*4,stride=(**pm).rowBytes&0x3fff,done=0;
    unsigned char *base=(unsigned char *)GetPixBaseAddr(pm);
    while(done<n) { long col=at%width,take=width-col;if(take>n-done)take=n-done;
        unsigned char *p=base+(at/width)*stride+col;
        if(intoWorld)memcpy(p,bytes+done,take);else memcpy(bytes+done,p,take);
        done+=take;at+=take;
    }
}
static OSErr thumbnail(GWorldPtr from,short i) {
    PixMapHandle a=GetGWorldPixMap(from),b;CGrafPtr saved;GDHandle device;
    Rect source=(**a).bounds,r=fit(source.right,source.bottom,320,240);GWorldPtr out=NULL;OSErr err;
    err=world(&out,&r);if(err)return err;b=GetGWorldPixMap(out);
    if(!LockPixels(a)) { DisposeGWorld(out);return memFullErr; }
    if(!LockPixels(b)) { UnlockPixels(a);DisposeGWorld(out);return memFullErr; }
    GetGWorld(&saved,&device);SetGWorld(out,NULL);ForeColor(blackColor);BackColor(whiteColor);
    CopyBits((BitMap *)*a,(BitMap *)*b,&source,&r,srcCopy,NULL);SetGWorld(saved,device);
    UnlockPixels(a);UnlockPixels(b);
    if(gImages[i].world)DisposeGWorld(gImages[i].world);gImages[i].world=out;
    gImages[i].width=r.right;gImages[i].height=r.bottom;gImages[i].used=++gClock;trim(i);return noErr;
}
void ChatImagesInit(ChatImageSendProc send,ChatImageNoticeProc notice) { gSend=send;gNotice=notice; }
Boolean ChatImagesPaste(char *outID) {
    Handle scrap=NewHandle(0);long offset=0,size;Rect source,target;PicHandle pic;
    CGrafPtr saved;GDHandle device;PixMapHandle pm;OSErr err;short i;unsigned long seconds;
    if(!scrap)return FALSE;size=GetScrap(scrap,'PICT',&offset);
    if(size<10) { DisposeHandle(scrap);return FALSE; }
    if(size>8L*1024L*1024L) { DisposeHandle(scrap);gNotice("Clipboard picture exceeds 8 MB");return TRUE; }
    if(gUpload) { DisposeHandle(scrap);gNotice("Wait for the current picture upload");return TRUE; }
    pic=(PicHandle)scrap;source=(**pic).picFrame;
    if(source.right<=source.left||source.bottom<=source.top||source.right-source.left>8192||source.bottom-source.top>8192) {
        DisposeHandle(scrap);gNotice("Clipboard picture has unsupported dimensions");return TRUE;
    }
    target=fit(source.right-source.left,source.bottom-source.top,768,768);
    err=world(&gUpload,&target);
    if(err) { DisposeHandle(scrap);gNotice("Not enough memory for clipboard picture");return TRUE; }
    pm=GetGWorldPixMap(gUpload);
    if(!LockPixels(pm)) { DisposeGWorld(gUpload);gUpload=NULL;DisposeHandle(scrap);return TRUE; }
    GetGWorld(&saved,&device);SetGWorld(gUpload,NULL);ForeColor(blackColor);BackColor(whiteColor);
    EraseRect(&target);DrawPicture(pic,&target);SetGWorld(saved,device);UnlockPixels(pm);DisposeHandle(scrap);
    GetDateTime(&seconds);sprintf(gUploadID,"img_%08lX_%08lX_%04lX",seconds,(unsigned long)TickCount(),++gSerial);
    i=slot(gUploadID,TRUE);err=thumbnail(gUpload,i);
    if(err) { DisposeGWorld(gUpload);gUpload=NULL;gNotice("Not enough memory for picture preview");return TRUE; }
    strcpy(outID,gUploadID);gPosition=0;gStarted=gEnded=FALSE;gNotice("Uploading picture...");return TRUE;
}
void ChatImagesTick(long space) {
    short chunks=0;char wire[1600],b64[CHUNK*2];unsigned char packed[CHUNK];PixMapHandle pm;long total;
    if(!gUpload||gEnded)return;pm=GetGWorldPixMap(gUpload);total=(**pm).bounds.right*4L*(**pm).bounds.bottom;
    if(!gStarted&&space>1600) { sprintf(wire,"G:BEGIN\t%s\t%d\t%d",gUploadID,(**pm).bounds.right,(**pm).bounds.bottom);gSend(wire);gStarted=TRUE;space-=160; }
    if(!gStarted||!LockPixels(pm))return;
    while(gPosition<total&&space>1600&&chunks++<16) { long n=total-gPosition;if(n>CHUNK)n=CHUNK;
        pixels(pm,gPosition,packed,n,FALSE);encode(packed,n,b64);
        sprintf(wire,"G:DATA\t%s\t%s",gUploadID,b64);gSend(wire);gPosition+=n;space-=strlen(wire)+1;
    }
    UnlockPixels(pm);
    if(gPosition==total&&space>160) { sprintf(wire,"G:END\t%s",gUploadID);gSend(wire);gEnded=TRUE; }
}
void ChatImagesDisconnect(void) {
    gPosition=0;gStarted=gEnded=FALSE;
    if(gIncoming)DisposeGWorld(gIncoming);gIncoming=NULL;
    for(short i=0;i<SLOTS;i++)gImages[i].asked=FALSE;
}
Boolean ChatImagesUploading(void) { return gUpload!=NULL; }
Boolean ChatImagesReady(const char *id) { short i=slot(id,FALSE);return i>=0&&gImages[i].ready; }
void ChatImagesCancel(const char *id) {
    if(gUpload&&!strcmp(id,gUploadID)) { char wire[100];sprintf(wire,"G:CANCEL\t%s",id);gSend(wire);DisposeGWorld(gUpload);gUpload=NULL; }
}
void ChatImagesLine(char *payload) {
    char *id=strchr(payload,'\t'),*data;short i;long n,total;PixMapHandle pm;unsigned char decoded[3072];
    if(!id)return;*id++=0;data=strchr(id,'\t');if(data)*data++=0;if(!valid_id(id))return;i=slot(id,TRUE);
    if(!strcmp(payload,"READY")) {
        gImages[i].ready=TRUE;
        if(gUpload&&!strcmp(id,gUploadID)) { DisposeGWorld(gUpload);gUpload=NULL;gNotice("Picture ready"); }
    } else if(!strcmp(payload,"ERROR")) {
        gImages[i].asked=TRUE;
        if(gUpload&&!strcmp(id,gUploadID)) { DisposeGWorld(gUpload);gUpload=NULL;gNotice(data?data:"Picture upload failed"); }
    } else if(!strcmp(payload,"VIEW")&&data) {
        short w=atoi(data),h=0;char *p=strchr(data,'\t');Rect r;
        if(p)h=atoi(p+1);if(w<1||w>320||h<1||h>240)return;
        if(gIncoming)DisposeGWorld(gIncoming);gIncoming=NULL;SetRect(&r,0,0,w,h);
        if(world(&gIncoming,&r))return;strcpy(gIncomingID,id);gIncomingPosition=0;
    } else if(!strcmp(payload,"DATA")&&data&&gIncoming&&!strcmp(id,gIncomingID)) {
        pm=GetGWorldPixMap(gIncoming);total=(**pm).bounds.right*4L*(**pm).bounds.bottom;
        n=decode(data,decoded,sizeof decoded);if(n<0||n>total-gIncomingPosition)return;
        if(LockPixels(pm)) { pixels(pm,gIncomingPosition,decoded,n,TRUE);UnlockPixels(pm);gIncomingPosition+=n; }
    } else if(!strcmp(payload,"DONE")&&gIncoming&&!strcmp(id,gIncomingID)) {
        pm=GetGWorldPixMap(gIncoming);total=(**pm).bounds.right*4L*(**pm).bounds.bottom;
        if(gIncomingPosition!=total) { DisposeGWorld(gIncoming);gIncoming=NULL;return; }
        if(gImages[i].world)DisposeGWorld(gImages[i].world);gImages[i].world=gIncoming;gIncoming=NULL;
        gImages[i].width=(**pm).bounds.right;gImages[i].height=(**pm).bounds.bottom;
        gImages[i].ready=TRUE;gImages[i].asked=FALSE;gImages[i].used=++gClock;trim(i);gNotice(NULL);
    }
}
short ChatImagesHeight(const char *id,short width) {
    short i=slot(id,FALSE);Rect r=fit(i>=0&&gImages[i].width?gImages[i].width:320,
        i>=0&&gImages[i].height?gImages[i].height:240,width,240);return r.bottom;
}
void ChatImagesDraw(const char *id,const Rect *box) {
    short i=slot(id,TRUE);PixMapHandle pm;Rect source,dest;GrafPtr port;RGBColor fore,back;
    if(!gImages[i].world) {
        if(!gImages[i].asked) { char wire[100];sprintf(wire,"G:FETCH\t%s",id);gSend(wire);gImages[i].asked=TRUE; }
        MoveTo(box->left+4,box->top+14);DrawText("Loading picture...",0,18);return;
    }
    pm=GetGWorldPixMap(gImages[i].world);source=(**pm).bounds;
    dest=fit(source.right,source.bottom,box->right-box->left,box->bottom-box->top);OffsetRect(&dest,box->left,box->top);
    if(!LockPixels(pm))return;GetPort(&port);GetForeColor(&fore);GetBackColor(&back);
    ForeColor(blackColor);BackColor(whiteColor);
    CopyBits((BitMap *)*pm,(BitMap *)*((CGrafPtr)port)->portPixMap,&source,&dest,srcCopy,NULL);FrameRect(&dest);
    RGBForeColor(&fore);RGBBackColor(&back);UnlockPixels(pm);gImages[i].used=++gClock;
}
void ChatImagesClose(void) {
    short i;if(gUpload)DisposeGWorld(gUpload);gUpload=NULL;
    if(gIncoming)DisposeGWorld(gIncoming);gIncoming=NULL;
    for(i=0;i<SLOTS;i++)if(gImages[i].world) { DisposeGWorld(gImages[i].world);gImages[i].world=NULL; }
}
OSErr ChatImagesTestScrap(void) {
    /* Fixture used only by the VMTEST caller. The user's clipboard is saved
     * and restored by the native UI test harness around this operation. */
    Rect r={0,0,64,97};PicHandle pic;RGBColor color;short x,y;long size;OSErr err;
    GWorldPtr offscreen=NULL;CGrafPtr saved;GDHandle device;
    if(!gScrapSaved) { short i;long offset;
        for(i=0;i<5;i++) { gSavedScrap[i]=NewHandle(0);if(gSavedScrap[i]&&GetScrap(gSavedScrap[i],kScrapTypes[i],&offset)<0) { DisposeHandle(gSavedScrap[i]);gSavedScrap[i]=NULL; } }
        gScrapSaved=TRUE;
    }
    err=world(&offscreen,&r);if(err)return err;
    if(!LockPixels(GetGWorldPixMap(offscreen))) { DisposeGWorld(offscreen);return memFullErr; }
    GetGWorld(&saved,&device);SetGWorld(offscreen,NULL);ForeColor(blackColor);BackColor(whiteColor);
    pic=OpenPicture(&r);if(!pic) { SetGWorld(saved,device);UnlockPixels(GetGWorldPixMap(offscreen));DisposeGWorld(offscreen);return memFullErr; }
    for(y=0;y<64;y+=8)for(x=0;x<96;x+=8) { Rect tile={y,x,y+8,x+8};
        color.red=x*600;color.green=y*900;color.blue=40000;RGBForeColor(&color);PaintRect(&tile);
    }
    ClosePicture();SetGWorld(saved,device);UnlockPixels(GetGWorldPixMap(offscreen));DisposeGWorld(offscreen);size=GetHandleSize((Handle)pic);HLock((Handle)pic);
    err=ZeroScrap();if(!err)err=PutScrap(size,'PICT',*(Handle)pic);HUnlock((Handle)pic);KillPicture(pic);return err;
}
OSErr ChatImagesRestoreScrap(void) {
    short i;OSErr err;if(!gScrapSaved)return noErr;err=ZeroScrap();
    for(i=0;i<5;i++)if(gSavedScrap[i]) { HLock(gSavedScrap[i]);
        if(!err)err=PutScrap(GetHandleSize(gSavedScrap[i]),kScrapTypes[i],*gSavedScrap[i]);
        HUnlock(gSavedScrap[i]);DisposeHandle(gSavedScrap[i]);gSavedScrap[i]=NULL;
    } gScrapSaved=FALSE;return err;
}
