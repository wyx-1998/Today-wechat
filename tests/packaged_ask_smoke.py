"""Actual sidecar JSONL events + recovery using owned data; never calls a model."""
import argparse
import json
import queue
import subprocess
import tempfile
import threading
from pathlib import Path
from backend.service import LocalService


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--binary',required=True);args=parser.parse_args()
    binary=Path(args.binary).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='ask-package-owned-') as root:
        service=LocalService(root);service.connect_demo();service.store.update_chat('demo_project@chatroom',sync_enabled=True,analysis_enabled=True);service.sync();service.store.set_paused(True)
        config=service.ask.models.save(base_url='https://owned.invalid/v1',model='owned',api_key='owned-secret-no-real-service',protocol='openai');service.close()
        process=subprocess.Popen([str(binary),'--data-dir',root],cwd=root,env={'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8','TMPDIR':root},stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
        inbox=queue.Queue();packets=[];counter=0
        def read():
            for line in process.stdout:inbox.put(json.loads(line))
        reader=threading.Thread(target=read,daemon=True);reader.start()
        def receive():
            value=inbox.get(timeout=15)
            if 'stream_id' in value:packets.append(value)
            return value
        def request(command,params=None,allow_error=False):
            nonlocal counter
            counter+=1;rid='request-'+str(counter)
            process.stdin.write(json.dumps({'version':1,'id':rid,'command':command,'params':params or {}})+'\n');process.stdin.flush()
            while True:
                value=receive()
                if value.get('id')==rid:
                    if not allow_error:assert 'error' not in value,value.get('error')
                    return value.get('result',value.get('error'))
        try:
            state=request('state');assert state['paused'];assert state['ask_model']['protocol']=='openai';assert 'owned-secret-no-real-service' not in json.dumps(state)
            p=request('ask.preview',{'question':'zzneverexistszz'});assert p['coverage']['evidence_messages']==0
            denied=request('ask.start',{'preview_token':p['preview_token']},True);assert denied['code']=='ask_authorization_required'
            started=request('ask.start',{'preview_token':p['preview_token'],'authorize':True})
            while not any(p['event']['type'] in {'RUN_FINISHED','RUN_ERROR'} for p in packets):receive()
            assert packets[-1]['event']['type']=='RUN_FINISHED';assert [p['sequence'] for p in packets]==list(range(1,len(packets)+1))
            assert len({p['run_id'] for p in packets})==1;assert all(p['stream_id']=='request-4' for p in packets)
            t=request('ask.detail',{'ask_thread_id':started['ask_thread_id']});assert t['turns'][0]['answer_status']=='no_evidence'
            assert request('ask.model.calls')['total']==0;assert request('model.calls')['total']==0
            assert request('state')['message_count']==state['message_count']
            Path('output/ask-events.json').write_text(json.dumps(packets,ensure_ascii=False))
            process.stdin.close();assert process.wait(timeout=10)==0;reader.join(timeout=2);assert process.stderr.read()==''
            print(json.dumps({'packaged_ask':'passed','events':len(packets),'fts5':'passed','unauthorized_send':'blocked','model_calls':0,'clean_exit':True}))
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=10)
            for stream in (process.stdin,process.stdout,process.stderr):
                if not stream.closed:stream.close()

if __name__=='__main__':main()
