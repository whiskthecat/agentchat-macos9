#!/usr/bin/env python3
"""PICT-rendered pixel uploads and image turns with recording fakes only."""
import base64
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from PIL import Image
from chat_images import Upload, ImageStore


def until(check):
    end=time.monotonic()+8
    while time.monotonic()<end:
        if check(): return
        time.sleep(.05)
    raise AssertionError('Timed out')


def main():
    with tempfile.TemporaryDirectory(prefix='agentchat-images-') as tmp:
        with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        os.environ.update(AGENT_TEST_MODE='1',CODEX_ENABLED='1',STUB_PORT=str(port),OPENCODE_URL=f'http://127.0.0.1:{port}',
                          AGENT_STATE_FILE=tmp+'/images.sqlite3',STUB_CODEX_STATE_FILE=tmp+'/codex.json')
        upstream=subprocess.Popen([sys.executable,str(Path(__file__).with_name('stub_opencode.py')),str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def state():
            try: return json.load(urllib.request.urlopen(os.environ['OPENCODE_URL']+'/state',timeout=.3))
            except OSError: return None
        try:
            until(state)
            import agentd as relay
            lines=[]; session=relay.Session('image-qa',lines.append)
            relay.model_catalog()
            muse,sol='opencode/muse-spark-1.3-contributor-free','codex/gpt-6.1-sol'
            # Odd width exercises row padding independently of native QuickDraw.
            image=Image.new('RGB',(97,64),(200,30,140)); raw=image.tobytes('raw','XRGB')
            def upload(image_id):
                session.image_line(f'BEGIN\t{image_id}\t97\t64')
                for at in range(0,len(raw),1024): session.image_line('DATA\t'+image_id+'\t'+base64.b64encode(raw[at:at+1024]).decode())
                session.image_line('END\t'+image_id)
                assert any(s.startswith('G:READY\t'+image_id) for s in lines)
            upload('img_Muse_fixture')
            assert state()['sessions']==0 and state()['prompts']==[]
            stored=relay.IMAGES.get('img_Muse_fixture','image-qa')
            with Image.open(io.BytesIO(stored['png'])) as decoded: assert decoded.size==(97,64) and decoded.getpixel((96,63))==(200,30,140)
            try: relay.IMAGES.get('img_Muse_fixture','different-peer')
            except ValueError: pass
            else: raise AssertionError('Cross-peer upload reuse accepted')
            session.image_line('BEGIN\timg_Bad\t9000\t1')
            assert lines[-1].startswith('G:ERROR\timg_Bad')
            incomplete=Upload('img_incomplete',1,1)
            try: relay.IMAGES.save(incomplete,'image-qa')
            except ValueError: pass
            else: raise AssertionError('Partial image committed')
            assert relay.model_info('strix/halogen-qwen3.8-flash-next')['images'], 'Qwen fixture must match its real image capability'
            relay.MODELS['qa/text-only']={'id':'qa/text-only','name':'Text-only QA','backend':'opencode','images':False}
            session.select_model('qa/text-only')
            session.image_attachment='img_Muse_fixture'
            try: session.prompt('unsupported vision')
            except ValueError: pass
            else: raise AssertionError('Non-vision model accepted a picture')
            assert state()['sessions']==0 and state()['prompts']==[]
            session.select_model(muse); session._start_sse(); time.sleep(.15)
            session.image_attachment='img_Muse_fixture'; session.prompt('describe the fixture')
            until(lambda:not session.inflight); muse_sid=session.sid
            part=state()['images'][-1]['part']
            assert part['mime']=='image/png' and part['url'].startswith('data:image/png;base64,')
            lines.clear(); session.select(muse_sid)
            assert 'G:ROW\timg_Muse_fixture' in lines and any(s.startswith('G:VIEW\timg_Muse_fixture') for s in lines)
            upload('img_Sol_fixture'); session.select_model(sol)
            session.image_attachment='img_Sol_fixture'; time.sleep(.6); session.prompt('')
            until(lambda:not session.inflight); sol_sid=session.sid
            fake=json.loads(Path(tmp+'/codex.json').read_text())
            assert fake['images'][-1]['input']['type']=='image' and fake['images'][-1]['input']['url'].startswith('data:image/png;base64,')
            persistent=ImageStore(tmp+'/images.sqlite3'); assert persistent.parts(relay.STORE.history(sol_sid)[0]['info']['id'])
            persistent.db.close()
            lines.clear(); session.select(sol_sid)
            assert 'G:ROW\timg_Sol_fixture' in lines, 'Image-only history was lost'
            session.delete(sol_sid); session.select_model(muse); session.delete(muse_sid)
            for image_id in ('img_Muse_fixture','img_Sol_fixture'):
                try: relay.IMAGES.get(image_id)
                except ValueError: pass
                else: raise AssertionError('Deleted conversation retained its image asset')
            upload('img_Qwen_fixture'); session.select_model('strix/halogen-qwen3.8-flash-next')
            session.image_attachment='img_Qwen_fixture'; time.sleep(.6); session.prompt('Qwen picture fixture')
            until(lambda:not session.inflight); qwen_sid=session.sid
            assert state()['images'][-1]['sid']==qwen_sid and state()['images'][-1]['part']['mime']=='image/png'
            assert state()['models'][-1]['model']=={'providerID':'strix','modelID':'halogen-qwen3.8-flash-next'}
            lines.clear(); session.select(qwen_sid)
            assert 'G:ROW\timg_Qwen_fixture' in lines
            session.delete(qwen_sid)
            assert len(state()['prompts'])==2 and len(fake['prompts'])==1 and fake['test_only']
            session.alive=False; relay.CODEX.close()
            print('PASS: odd-width pixels, bounded/partial uploads, peer ownership, text-only rejection before inference, Muse/Qwen PNG routing, Sol image-only routing, visible persistent history and asset deletion; 3 fake turns, 0 real turns')
        finally: upstream.terminate(); upstream.wait(timeout=5)


if __name__=='__main__': main()
