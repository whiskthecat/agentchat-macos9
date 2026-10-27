#!/usr/bin/env python3
"""Receive externally started turns without inference; recording backends only."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    with tempfile.TemporaryDirectory(prefix='agentchat-wake-') as tmp:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        os.environ.update(AGENT_TEST_MODE='1',CODEX_ENABLED='1',STUB_PORT=str(port),
                          OPENCODE_URL=f'http://127.0.0.1:{port}',AGENT_STATE_FILE=tmp+'/state.sqlite3',
                          STUB_CODEX_STATE_FILE=tmp+'/codex.json')
        process=subprocess.Popen([sys.executable,str(Path(__file__).with_name('stub_opencode.py'))],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        checks=0
        def check(name,condition):
            nonlocal checks
            assert condition,name
            checks+=1;print('PASS '+name,flush=True)
        try:
            for _ in range(100):
                try:
                    with urllib.request.urlopen(os.environ['OPENCODE_URL']+'/state',timeout=.3) as r:json.load(r)
                    break
                except OSError:time.sleep(.03)
            else:raise RuntimeError('Recording backend did not start')
            import agentd as relay
            relay.model_catalog();lines=[];s=relay.Session('wake-qa',lines.append)
            s.select_model('strix/halogen-qwen3.8-flash-next');s._lazy_create('wake fixture');sid=s.sid
            def event(kind,props):s._event({'type':kind,'properties':props})
            def text(mid,body):
                event('message.updated',{'info':{'id':mid,'sessionID':sid,'role':'assistant'}})
                event('message.part.updated',{'part':{'id':mid+'_text','sessionID':sid,'messageID':mid,'type':'text'}})
                event('message.part.delta',{'sessionID':sid,'messageID':mid,'partID':mid+'_text','field':'text','delta':body})
            lines.clear();text('late','ignored')
            check('Idle late text is suppressed',not lines)
            event('session.status',{'sessionID':'ses_foreign','status':{'type':'busy'}})
            check('Foreign session cannot activate this chat',not s.inflight)
            event('session.status',{'sessionID':sid,'status':{'type':'busy'}})
            check('Upstream wake activates without a prompt',s.inflight and lines==['S:thinking...'])
            event('session.status',{'sessionID':sid,'status':{'type':'busy'}})
            check('Repeated busy events do not restart native UI',lines==['S:thinking...'])
            text('awake','I woke up.')
            check('Woken text reaches the socket',lines[-1]=='T:I woke up.')
            event('session.status',{'sessionID':sid,'status':{'type':'idle'}})
            event('session.idle',{'sessionID':sid})
            check('Duplicate idle events complete only once',not s.inflight and lines.count('D:')==1)
            lines.clear();event('session.status',{'sessionID':sid,'status':{'type':'retry'}})
            check('Provider retry keeps the native chat busy',s.inflight)
            # Explicit cancellation remains terminal until authoritative idle.
            s.abort('recording stop');lines.clear()
            event('session.status',{'sessionID':sid,'status':{'type':'busy'}});text('after_stop','ignored')
            check('Stopped-turn status and deltas stay suppressed',not lines and not s.inflight)
            event('session.status',{'sessionID':sid,'status':{'type':'idle'}})
            event('session.status',{'sessionID':sid,'status':{'type':'busy'}});text('next_wake','Next wake')
            check('A subsequent real wake is accepted after idle',lines[-1]=='T:Next wake')
            event('session.idle',{'sessionID':sid});lines.clear()
            # Recover a busy external turn when reselecting/reconnecting.
            request=urllib.request.Request(os.environ['OPENCODE_URL']+'/test/stream',
                     data=json.dumps({'sid':sid,'text':'Recovered external reply '*30,'delay':.2,'chunk':8}).encode(),
                     headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as r:json.load(r)
            time.sleep(.05);s.select(sid)
            check('Selecting a busy upstream chat restores busy state',s.inflight and 'S:thinking...' in lines)
            s.abort('end recording stream')
            s.select_model('codex/gpt-6.1-sol');s._lazy_create('Codex wake fixture');lines.clear()
            thread=s.codex_thread
            s._codex_event('turn/started',{'threadId':'other','turn':{'id':'foreign'}})
            check('Foreign Codex thread cannot wake this chat',not s.inflight)
            s._codex_event('turn/started',{'threadId':thread,'turn':{'id':'wake_turn'}})
            s._codex_event('item/agentMessage/delta',{'threadId':thread,'turnId':'wake_turn','itemId':'wake_text','delta':'Codex woke'})
            s._codex_event('turn/completed',{'threadId':thread,'turn':{'id':'wake_turn','status':'completed'}})
            check('Codex turn-start/text/completion need no Send',lines==['S:thinking...','B:6.1 Sol','T:Codex woke','D:'])
            check('Woken Codex text persists in history',relay.STORE.history(s.sid)[0]['parts'][0]['text']=='Codex woke')
            lines.clear();s._codex_event('turn/started',{'threadId':thread,'turn':{'id':'wake_turn'}})
            check('Retired Codex turns cannot restart the UI',not s.inflight and not lines)
            with urllib.request.urlopen(os.environ['OPENCODE_URL']+'/state') as r:state=json.load(r)
            codex_state = json.loads(Path(tmp+'/codex.json').read_text()) if Path(tmp+'/codex.json').exists() else {'prompts':[]}
            check('All wake/recovery checks submit zero inference',state['prompts']==[] and codex_state['prompts']==[])
            s.alive=False;relay.CODEX.close()
            print(f'PASS: {checks} checks; 0 inference requests, recording backends only')
        finally:process.terminate();process.wait(timeout=5)


if __name__=='__main__':main()
