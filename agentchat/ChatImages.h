#ifndef CHAT_IMAGES_H
#define CHAT_IMAGES_H
#include <Types.h>
#include <Quickdraw.h>
typedef void (*ChatImageSendProc)(const char *);
typedef void (*ChatImageNoticeProc)(const char *);
void ChatImagesInit(ChatImageSendProc, ChatImageNoticeProc);
Boolean ChatImagesPaste(char *imageID);
void ChatImagesLine(char *payload);
void ChatImagesTick(long queueSpace);
void ChatImagesDisconnect(void);
void ChatImagesCancel(const char *imageID);
Boolean ChatImagesUploading(void);
Boolean ChatImagesReady(const char *imageID);
void ChatImagesDraw(const char *imageID, const Rect *box);
short ChatImagesHeight(const char *imageID, short width);
void ChatImagesClose(void);
OSErr ChatImagesTestScrap(void);
OSErr ChatImagesRestoreScrap(void);
#endif
