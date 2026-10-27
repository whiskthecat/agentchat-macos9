#!/usr/bin/env python3
"""Assistant pictures and MCP delivery, isolated recording backends only."""
import base64
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from PIL import Image
from chat_images import ImageStore
from remote_mac import MacTools


def main():
    checks = []
    def check(label, condition):
        assert condition, label
        checks.append(label)
        print('PASS ' + label, flush=True)

    with tempfile.TemporaryDirectory(prefix='agentchat-agent-images-') as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        os.environ.update(AGENT_TEST_MODE='1', CODEX_ENABLED='1',
                          STUB_PORT=str(port),
                          OPENCODE_URL=f'http://127.0.0.1:{port}',
                          AGENT_STATE_FILE=tmp+'/images.sqlite3',
                          STUB_CODEX_STATE_FILE=tmp+'/codex.json')
        upstream = subprocess.Popen([sys.executable, str(Path(__file__).with_name('stub_opencode.py')), str(port)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(os.environ['OPENCODE_URL']+'/state', timeout=.3) as response:
                        assert isinstance(json.load(response)['prompts'], list)
                    break
                except OSError: time.sleep(.05)
            else: raise AssertionError('Recording server did not start')
            import agentd as relay
            relay.model_catalog()
            lines = []; session = relay.Session('assistant-qa', lines.append)
            session.connection_id = 'qa_connection'
            tools = MacTools(None, session)
            image = Image.new('RGBA', (641, 97), (80, 170, 20, 255))
            image.putpixel((0, 0), (0, 0, 0, 0))
            path = Path(tmp)/'output.png'; image.save(path)
            def rejects(arguments, label):
                before = len(lines)
                try: tools.call('imac_send_image', arguments)
                except (ValueError, OSError, RuntimeError): pass
                else: raise AssertionError('Accepted: ' + label)
                check(label, len(lines) == before)

            for model in ('opencode/muse-spark-1.3-contributor-free', 'codex/gpt-6.1-sol'):
                session.select_model(model); session._lazy_create('assistant picture fixture')
                sid = session.sid; lines.clear()
                result = tools.call('imac_send_image', {'path': str(path), 'conversation': sid,
                                                        'caption': 'Here is your picture.'})['structuredContent']
                check(model + ': live preview and assistant caption',
                      'G:ROW\t'+result['image'] in lines and 'T:Here is your picture.' in lines
                      and lines.index('B:'+relay.model_info(model)['name']) < lines.index('G:ROW\t'+result['image']))
                preview = next(line for line in lines if line.startswith('G:VIEW\t')).split('\t')
                check(model + ': bounded QuickDraw preview', int(preview[2]) <= 320 and int(preview[3]) <= 240)
                asset = relay.IMAGES.get(result['image'], 'assistant-qa')
                with Image.open(io.BytesIO(asset['png'])) as decoded:
                    check(model + ': transparency uses white paper', decoded.getpixel((0, 0)) == (255, 255, 255))
                second = tools.call('imac_send_image', {'path': str(path), 'conversation': sid})['structuredContent']
                check(model + ': repeated source has distinct messages', second['message'] != result['message'])
                session.select(''); lines.clear(); session.select(sid)
                check(model + ': captioned and image-only replay', lines.count('G:ROW\t'+result['image']) == 2
                      and 'V:TEXT\tHere is your picture.' in lines)
                persisted = ImageStore(tmp+'/images.sqlite3')
                check(model + ': messages survive relay recreation', len(persisted.messages(sid)) == 2)
                persisted.db.close()
                rejects({'path': str(path), 'conversation': 'ses_stale'}, 'stale conversation rejected')
                session.connection_id = 'new_connection'
                rejects({'path': str(path), 'conversation': sid}, 'stale connection rejected')
                session.connection_id = 'qa_connection'
                for source in ('relative.png', tmp):
                    rejects({'path': source, 'conversation': sid}, 'non-file source rejected: '+source)
                bad = Path(tmp)/'bad.png'; bad.write_bytes(b'not an image')
                rejects({'path': str(bad), 'conversation': sid}, 'invalid picture rejected')
                huge = Path(tmp)/'too-many-pixels.png'
                Image.new('1', (4097,4097)).save(huge)
                rejects({'path':str(huge),'conversation':sid}, 'oversized raster rejected before decoding')
                huge = Path(tmp)/'too-many-bytes.png'
                with huge.open('wb') as stream: stream.truncate(16*1024*1024+1)
                rejects({'path':str(huge),'conversation':sid}, 'oversized source rejected')
                rejects({'path': str(path), 'conversation': sid, 'surprise': True}, 'unknown tool argument rejected')

            class NativeFileTools(MacTools):
                def read(self, path, fork='data', offset=0, limit=0):
                    assert path == 'Macintosh HD:Desktop Folder:picture.gif' and fork == 'data'
                    return gif.getvalue(), len(gif.getvalue())
            gif = io.BytesIO()
            Image.new('RGB', (12, 9), 'red').save(gif, format='GIF', save_all=True,
                                                append_images=[Image.new('RGB', (12, 9), 'blue')])
            result = NativeFileTools(None, session).call('imac_send_image',
                {'source': 'mac', 'path': 'Macintosh HD:Desktop Folder:picture.gif', 'conversation': session.sid})['structuredContent']
            with Image.open(io.BytesIO(relay.IMAGES.get(result['image'])['png'])) as decoded:
                check('Macintosh data fork and GIF first frame', decoded.getpixel((0, 0)) == (255, 0, 0))

            # Exercise actual OpenCode event filtering and repeated image parts.
            session.select_model('opencode/muse-spark-1.3-contributor-free')
            session.inflight = True; sid = session.sid
            session._event({'type': 'message.updated', 'properties': {'info':
                            {'id': 'msg_picture', 'sessionID': sid, 'role': 'assistant'}}})
            part = {'type': 'file', 'id': 'prt_picture', 'sessionID': sid, 'messageID': 'msg_picture',
                    'mime': 'image/png', 'url': 'data:image/png;base64,'+base64.b64encode(path.read_bytes()).decode()}
            lines.clear()
            event = {'type': 'message.part.updated', 'properties': {'part': part}}
            session._event(event); session._event(event)
            check('Embedded assistant image streamed exactly once', sum(line.startswith('G:ROW\t') for line in lines) == 1)
            check('Embedded image bound to upstream message', len(relay.IMAGES.parts('msg_picture')) == 1)
            lines.clear(); foreign = dict(part, sessionID='ses_other')
            session._event({'type': 'message.part.updated', 'properties': {'part': foreign}})
            session.inflight = False; session._event(event)
            check('Foreign and stopped-turn pictures ignored', not lines)
            check('Remote image URLs never fetched', relay.IMAGES.import_part(dict(part, url='https://example.invalid/a.png'), session.key) is None)
            # Selection must wait for the complete preview/row, otherwise the
            # native client's single incoming GWorld can land in another chat.
            began = threading.Event(); release = threading.Event(); switched = threading.Event()
            errors = []
            lines.clear()
            def delayed_send(line):
                lines.append(line)
                if line.startswith('G:VIEW\t'):
                    began.set()
                    if not release.wait(3): errors.append('Preview gate timed out')
            session.send = delayed_send
            def publishing():
                try: tools.call('imac_send_image', {'path':str(path),'conversation':sid})
                except Exception as error: errors.append(str(error))
            def switching():
                try: session.select(''); switched.set()
                except Exception as error: errors.append(str(error))
            publisher = threading.Thread(target=publishing); selector = threading.Thread(target=switching)
            publisher.start()
            try:
                assert began.wait(2)
                selector.start(); time.sleep(.05)
                check('Conversation switching waits for the complete picture', not switched.is_set())
            finally:
                release.set(); publisher.join(3)
                if selector.ident: selector.join(3)
                session.send = lines.append
            check('Picture cannot leak into the newly selected chat', not errors and switched.is_set()
                  and next(i for i,line in enumerate(lines) if line.startswith('G:ROW\t'))
                  < next(i for i,line in enumerate(lines) if line.startswith('V:BEGIN\t')))
            # Global asset deletion retains pictures shared by another chat.
            first_sid = sid
            relay.IMAGES.delete_session(first_sid)
            check('Deleting one chat retains another chat\'s picture', relay.IMAGES.get(result['image']) is not None)
            relay.IMAGES.delete_session(relay.STORE.last(session.key, 'codex/gpt-6.1-sol'))
            check('Deletion removes assistant message metadata', not relay.IMAGES.messages(first_sid))
            with urllib.request.urlopen(os.environ['OPENCODE_URL']+'/state') as response: state = json.load(response)
            fake_path = Path(tmp+'/codex.json')
            fake = json.loads(fake_path.read_text()) if fake_path.exists() else {'prompts': [], 'test_only': True}
            check('No inference needed to send, replay or delete pictures', state['prompts'] == [] and fake['prompts'] == [] and fake['test_only']
                  and relay.CODEX.command[1].endswith('stub_codex.py'))
            session.alive = False; relay.CODEX.close()
            print(f'PASS: {len(checks)} checks, 0 fake inference turns, 0 real inference turns')
        finally:
            upstream.terminate(); upstream.wait(timeout=5)


if __name__ == '__main__': main()
