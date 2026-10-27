"""Bounded native picture uploads, PNG attachments and QuickDraw previews.

G: traffic only stores/previews images. P: is still the only inference trigger.
The Mac renders its native PICT clipboard into XRGB; no host PICT decoder is
needed. Image assets persist in the lane's existing SQLite database.
"""
import base64
import io
import hashlib
import re
import sqlite3
import threading
import time
import uuid
from PIL import Image

MAX_EDGE = 768
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_PIXELS = 16 * 1024 * 1024
ID = re.compile(r"img_[A-Za-z0-9_]{1,60}\Z")


class Upload:
    def __init__(self, image_id, width, height):
        if not ID.fullmatch(image_id) or not 1 <= width <= MAX_EDGE or not 1 <= height <= MAX_EDGE:
            raise ValueError("Invalid picture ID or dimensions")
        self.id, self.width, self.height = image_id, width, height
        self.data=bytearray(); self.total=width*height*4; self.used=time.monotonic()

    def chunk(self, image_id, encoded):
        if image_id != self.id: raise ValueError("Picture upload ID changed")
        data=base64.b64decode(encoded,validate=True)
        if not data or len(data)>3072 or len(self.data)+len(data)>self.total:
            raise ValueError("Invalid picture chunk")
        self.data.extend(data); self.used=time.monotonic()

    def image(self):
        if len(self.data)!=self.total: raise ValueError("Incomplete picture upload")
        return Image.frombytes("RGB",(self.width,self.height),bytes(self.data),"raw","XRGB",self.width*4)


class ImageStore:
    def __init__(self,path):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS image_assets (
                    id TEXT PRIMARY KEY, owner TEXT, png BLOB, width INTEGER,
                    height INTEGER, created INTEGER);
                CREATE TABLE IF NOT EXISTS message_images (
                    mid TEXT, sid TEXT, asset TEXT, PRIMARY KEY(mid,asset));
                CREATE TABLE IF NOT EXISTS image_messages (
                    mid TEXT PRIMARY KEY, sid TEXT, created INTEGER,
                    speaker TEXT, caption TEXT);
            """)

    def get(self,image_id,owner=None):
        with self.lock:
            row=self.db.execute("SELECT * FROM image_assets WHERE id=?",(image_id,)).fetchone()
            if not row or owner is not None and row['owner']!=owner:
                raise ValueError("Picture is unavailable on this relay")
            return dict(row)

    def save(self,upload,owner):
        img=upload.image(); out=io.BytesIO(); img.save(out,format='PNG'); png=out.getvalue()
        with self.lock,self.db:
            old=self.db.execute("SELECT owner,png FROM image_assets WHERE id=?",(upload.id,)).fetchone()
            if old:
                if old['owner']!=owner or old['png']!=png: raise ValueError("Picture ID already contains different data")
            else:
                self.db.execute("INSERT INTO image_assets VALUES (?,?,?,?,?,?)",
                                (upload.id,owner,png,img.width,img.height,int(time.time())))
        return self.get(upload.id,owner)

    def part(self,image_id,owner=None):
        asset=self.get(image_id,owner)
        return {'type':'file','mime':'image/png','filename':image_id+'.png',
                'url':'data:image/png;base64,'+base64.b64encode(asset['png']).decode()}

    def import_bytes(self, data, owner):
        """Normalize an agent's raster into the same bounded native asset format."""
        if not data or len(data) > MAX_SOURCE_BYTES:
            raise ValueError("Picture must contain 1 byte to 16 MiB")
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_SOURCE_PIXELS:
                raise ValueError("Picture exceeds 16 million pixels")
            source.seek(0)  # Animated GIFs are represented by their first frame.
            source.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            rgba = source.convert('RGBA')
            image = Image.new('RGB', rgba.size, 'white')
            image.paste(rgba, mask=rgba.getchannel('A'))
        image_id = 'img_agent_' + hashlib.sha256(owner.encode() + b'\0' + data).hexdigest()[:32]
        upload = Upload(image_id, image.width, image.height)
        upload.data = bytearray(image.tobytes('raw', 'XRGB'))
        return self.save(upload, owner)

    def import_part(self, part, owner):
        image_id = self.id_for_part(part)
        if image_id: return image_id
        if part.get('type') != 'file' or not part.get('mime', '').startswith('image/'):
            return None
        url = part.get('url', '')
        if not isinstance(url, str) or not url.startswith('data:image/'):
            return None  # Remote URLs are never fetched implicitly.
        if len(url) > (MAX_SOURCE_BYTES + 2) // 3 * 4 + 128:
            raise ValueError('Embedded picture exceeds 16 MiB')
        header, encoded = url.split(',', 1)
        if not header.endswith(';base64'): raise ValueError('Picture must use base64 data')
        return self.import_bytes(base64.b64decode(encoded, validate=True), owner)['id']

    def publish(self, sid, asset, speaker, caption=''):
        """Persist a separate assistant message without modifying upstream history."""
        mid = 'image_' + uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute('INSERT INTO image_messages VALUES (?,?,?,?,?)',
                            (mid, sid, int(time.time() * 1000), speaker, caption))
            self.db.execute('INSERT INTO message_images VALUES (?,?,?)', (mid, sid, asset))
        return mid

    def messages(self, sid):
        with self.lock:
            rows = list(self.db.execute('SELECT * FROM image_messages WHERE sid=? ORDER BY rowid', (sid,)))
        return [{'info': {'id': r['mid'], 'role': 'assistant', 'speaker': r['speaker'],
                          'time': {'created': r['created']}},
                 'parts': [{'type': 'text', 'text': r['caption']}]} for r in rows]

    def bind(self,mid,sid,image_id):
        with self.lock,self.db:
            self.db.execute("INSERT OR IGNORE INTO message_images VALUES (?,?,?)",(mid,sid,image_id))

    def parts(self,mid):
        with self.lock:
            ids=[r[0] for r in self.db.execute("SELECT asset FROM message_images WHERE mid=? ORDER BY rowid",(mid,))]
        return [self.part(i) for i in ids]

    def id_for_part(self,part):
        name=part.get('filename','')
        image_id=name[:-4] if name.endswith('.png') else ''
        if part.get('type')!='file' or part.get('mime')!='image/png' or not ID.fullmatch(image_id): return None
        try: self.get(image_id)
        except ValueError: return None
        return image_id

    def show(self,image_id,send):
        asset=self.get(image_id)
        with Image.open(io.BytesIO(asset['png'])) as img:
            img=img.convert('RGB'); img.thumbnail((320,240),Image.Resampling.LANCZOS)
            raw=img.tobytes('raw','XRGB')
            send('G:VIEW\t%s\t%d\t%d' % (image_id,img.width,img.height))
            for at in range(0,len(raw),3072): send('G:DATA\t'+image_id+'\t'+base64.b64encode(raw[at:at+3072]).decode())
            send('G:DONE\t'+image_id)

    def delete_session(self,sid):
        with self.lock,self.db:
            assets=[row[0] for row in self.db.execute("SELECT asset FROM message_images WHERE sid=?",(sid,))]
            self.db.execute("DELETE FROM message_images WHERE sid=?",(sid,))
            self.db.execute("DELETE FROM image_messages WHERE sid=?",(sid,))
            for asset in assets:
                self.db.execute("DELETE FROM image_assets WHERE id=? AND NOT EXISTS (SELECT 1 FROM message_images WHERE asset=?)",(asset,asset))
