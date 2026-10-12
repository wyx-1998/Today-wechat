"""Isolated Ask behavior and HTTP adapters. Generated data, no external traffic."""
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch
import sqlite3

from backend.service import LocalService
from backend.storage import Store
from backend.ask_provider import AskProvider, StreamControl, openai_endpoint
from backend.ask_retrieval import tokenize
from backend.contracts import ServiceError, iso_time


def stamp(value): return datetime.fromisoformat(value+'+08:00').timestamp()


class Client:
    def __init__(self):
        self.calls=[];self.answer='方案需要周五提交。[M1]';self.error=None;self.callback=None;self.pause=None;self.entered=threading.Event()
    def stream(self,config,key,prompt,control):
        self.calls.append(json.loads(prompt.split('\n',1)[1]) if '\n' in prompt else prompt)
        self.entered.set()
        if self.callback:self.callback()
        yield {'text':self.answer[:8]}
        if self.pause:self.pause.wait(3)
        control.check()
        if self.error:raise self.error
        yield {'text':self.answer[8:]}
        yield {'finished':True,'usage':{'input_tokens':10,'output_tokens':8,'total_tokens':25}}


class AskTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(prefix='ask-owned-');self.addCleanup(self.root.cleanup)
        self.now=stamp('2026-10-10T12:00:00');self.client=Client()
        self.s=LocalService(self.root.name,clock=lambda:self.now,ask_client=self.client);self.addCleanup(self.s.close)
        source={'mode':'local','root':self.root.name,'account_id':'owned','self_id':'me'}
        self.s.store.configure(source,iso_time(stamp('2026-10-01T00:00:00')),[{'chat_id':c,'chat_name':'项目群'+c,'chat_type':'group'} for c in ('a','b','outside')])
        self.s.store.update_chat('a',sync_enabled=True,analysis_enabled=True)
        self.s.ask.models.save(base_url='https://owned.invalid/v1',model='owned-model',api_key='owned-secret-value',protocol='openai')
        self.scope={'chat_ids':['a'],'time_start':iso_time(stamp('2026-10-01T00:00:00')),'time_end':iso_time(self.now),'person':'','include_results':True}
        self.add('m1','小王说方案需要周五提交。电话13800138000')
        self.add('m2','回复：可以按这个方案做。',sender='me',name='本人')
        self.add('out','范围外秘密',chat='outside')
        self.network=patch('socket.socket',side_effect=AssertionError('No external network'));self.network.start();self.addCleanup(self.network.stop)
    def add(self,mid,content,chat='a',sender='person',name='小王',day='2026-10-09T12:00:00',kind='text',account='owned'):
        with self.s.store.database() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(mid,account,chat,'local',mid,sender,name,None,iso_time(stamp(day)),stamp(day),kind,content,'text_available' if content is not None else 'type_only',int(kind=='text')))
    def preview(self,question='方案进展',scope=None):return self.s.ask.preview(question,scope or self.scope)
    def start(self,p=None,**kwargs):
        packets=[];done=threading.Event()
        def sink(packet):
            packets.append(packet)
            if packet['event']['type'] in {'RUN_FINISHED','RUN_ERROR'}:done.set()
        ids=self.s.dispatch('ask.start',{'preview_token':(p or self.preview())['preview_token'],**kwargs},event_sink=sink)
        return ids,packets,done
    def finish(self,ids,done):
        self.assertTrue(done.wait(4));return self.s.ask.detail(ids['ask_thread_id'])['turns'][-1]
    def tables(self):
        with self.s.store.database() as db:return {n:list(map(tuple,db.execute('SELECT * FROM '+n+' ORDER BY 1'))) for n in ('messages','tasks','attention_items','analysis_runs','text_grants','cursors')}
    def test_authorization_required_no_calls_or_turns(self):
        p=self.preview()
        with self.assertRaises(ServiceError) as e:self.start(p)
        self.assertEqual(e.exception.code,'ask_authorization_required');self.assertEqual(self.client.calls,[]);self.assertEqual(self.s.ask.list(),[])
    def test_native_config_is_independent_from_text_config_grant_revision_and_usage(self):
        self.s.models.save(base_url='https://text.invalid',model='text-model',api_key='text-secret')
        g=self.s.automatic.preview();self.s.automatic.save(authorize=True,preview_token=g['preview_token'])
        old=self.s.models.row();settings=self.s.store.settings();before=self.tables()
        self.s.ask.models.save(base_url='https://owned.invalid',model='native-model',api_key='new-secret',protocol='gemini')
        self.assertEqual(self.s.models.row(),old);self.assertEqual(self.s.store.settings()['text_model_revision'],settings['text_model_revision'])
        self.assertNotIn('new-secret',json.dumps(self.s.state()))
        self.s.ask.models.enable(enabled=False);self.assertEqual(self.s.models.row(),old);self.assertEqual(self.tables(),before)
    def test_scope_precedes_search_and_index_Chinese_short_names(self):
        p=self.preview('小王')
        ids={e['message_id'] for e in p['evidence']}
        self.assertIn('m1',ids);self.assertNotIn('out',ids);self.assertIn('小王',tokenize('小王答应提交'))
        self.assertNotIn('13800138000',json.dumps(p))
    def test_incremental_index_updates_without_duplicates(self):
        self.preview();self.preview()
        with self.s.store.database() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ask_fts').fetchone()[0],3)
            db.execute("UPDATE messages SET content='新项目月报' WHERE message_id='m1'")
        p=self.preview('月报');self.assertIn('m1',{e['message_id'] for e in p['evidence']})
        with self.s.store.database() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ask_fts').fetchone()[0],3)
    def test_time_and_account_isolation(self):
        self.add('earlier','方案',day='2026-09-29T12:00:00');self.add('other','方案',account='other')
        self.assertEqual({e['message_id'] for e in self.preview()['evidence']},{'m1','m2'})
    def test_person_ambiguity_requires_explicit_account(self):
        self.add('same-name','方案',sender='another')
        with self.assertRaises(ServiceError) as e:self.preview(scope={**self.scope,'person':'小王'})
        self.assertEqual(e.exception.code,'ask_person_ambiguous')
        p=self.preview(scope={**self.scope,'person':'person'})
        self.assertEqual([e['message_id'] for e in p['evidence']],['m1'])
    def test_relative_last_week_intersects_scope(self):
        self.add('last-week','上周约定方案',day='2026-10-02T12:00:00')
        p=self.preview('上周全部约定');self.assertEqual([e['message_id'] for e in p['evidence']],['last-week'])
    def test_every_event_in_order_single_terminal_and_business_is_read_only(self):
        before=self.tables();ids,events,done=self.start(authorize=True);turn=self.finish(ids,done)
        self.assertEqual(turn['answer_status'],'answered');self.assertEqual(len(turn['citations']),1)
        self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))
        self.assertEqual(events[0]['event']['type'],'RUN_STARTED');self.assertEqual(events[-1]['event']['type'],'RUN_FINISHED')
        self.assertEqual(sum(e['event']['type'] in {'RUN_ERROR','RUN_FINISHED'} for e in events),1)
        self.assertEqual(before,self.tables());self.assertEqual(self.s.ask.models.usage()['today']['requests'],1);self.assertEqual(self.s.models.usage()['today']['requests'],0)
        self.assertNotIn('owned-secret-value',json.dumps(events))
    def test_bad_paragraph_does_not_hide_independent_valid_citation(self):
        self.client.answer='方案需要提交。[M1]\n\n这个结论无依据。[M999]'
        ids,events,done=self.start(authorize=True);t=self.finish(ids,done)
        self.assertEqual(t['answer_status'],'partial');self.assertEqual(len(t['citations']),1);self.assertIn('出处不可用',t['conclusion']);self.assertIn('未核实',t['conclusion'])
    def test_uncited_answer_marked_unverified(self):
        self.client.answer='全都完成了。';ids,_,done=self.start(authorize=True);t=self.finish(ids,done)
        self.assertEqual(t['answer_status'],'partial');self.assertEqual(t['citations'],[])
    def test_no_evidence_avoids_paid_request(self):
        ids,_,done=self.start(self.preview('从未出现的zzuniquezz'),authorize=True);t=self.finish(ids,done)
        self.assertEqual(t['answer_status'],'no_evidence');self.assertEqual(self.client.calls,[])
    def test_complete_lists_account_for_unreadable_and_over_limit(self):
        self.add('image',None,kind='image')
        p=self.preview('列出全部约定');self.assertFalse(p['coverage']['complete']);self.assertEqual(p['coverage']['total_messages'],3)
        for i in range(305):self.add('batch'+str(i),'方案约定')
        p=self.preview('列出全部约定');self.assertTrue(p['coverage']['truncated']);self.assertEqual(p['coverage']['evidence_messages'],300)
    def test_preview_freezes_evidence_new_sync_not_mixed(self):
        p=self.preview();self.add('new','新方案')
        ids,_,done=self.start(p,authorize=True);self.finish(ids,done)
        self.assertNotIn('new',{e['message_id'] for e in self.client.calls[0]['evidence']})
    def test_configuration_change_invalidates_preview_and_grant(self):
        p=self.preview();self.s.ask.models.save(base_url='https://owned.invalid/v1',model='owned-model',api_key='changed-secret',protocol='openai')
        with self.assertRaises(ServiceError):self.start(p,authorize=True)
        ids,_,done=self.start(authorize=True,persistent=True);self.finish(ids,done)
        self.assertTrue(self.preview()['authorized']);self.s.ask.models.enable(enabled=True);self.assertFalse(self.preview()['authorized'])
    def test_expanded_scope_needs_new_grant(self):
        ids,_,done=self.start(authorize=True,persistent=True);self.finish(ids,done)
        p=self.preview();self.assertTrue(p['authorized']);self.assertFalse(self.preview(scope={**self.scope,'chat_ids':['a','b']})['authorized'])
    def test_followups_only_include_same_scope_and_config(self):
        ids,_,done=self.start(authorize=True);self.finish(ids,done)
        ids2,_,done2=self.start(ask_thread_id=ids['ask_thread_id'],authorize=True);self.finish(ids2,done2)
        self.assertEqual(len(self.client.calls[1]['history']),1)
        ids3,_,done3=self.start(self.preview(scope={**self.scope,'chat_ids':['b']}),ask_thread_id=ids['ask_thread_id'],authorize=True);self.finish(ids3,done3)
        self.assertEqual(len(self.client.calls),2)
    def test_stop_immediate_persistence_late_answer_cannot_replace(self):
        self.client.pause=threading.Event();ids,events,done=self.start(authorize=True)
        self.assertTrue(self.client.entered.wait(2));self.s.ask.stop(ids['run_id']);self.assertEqual(self.s.ask.detail(ids['ask_thread_id'])['turns'][0]['error_code'],'ask_stopped')
        self.client.pause.set();t=self.finish(ids,done)
        self.assertEqual(t['answer_status'],'partial');self.assertNotEqual(t['conclusion'],self.client.answer);self.assertEqual(events[-1]['event']['type'],'RUN_ERROR')
    def test_revoke_during_generation_stops_only_Ask(self):
        self.client.pause=threading.Event();ids,_,done=self.start(authorize=True,persistent=True)
        self.assertTrue(self.client.entered.wait(2));self.s.ask.revoke();self.client.pause.set();t=self.finish(ids,done)
        self.assertEqual(t['error_code'],'ask_stopped');self.assertFalse(self.preview()['authorized']);self.assertFalse(self.s.stop_event.is_set())
    def test_one_run_per_thread_and_settings_not_blocked(self):
        self.client.pause=threading.Event();ids,_,done=self.start(authorize=True)
        self.assertTrue(self.client.entered.wait(2));self.assertIn('ask_model',self.s.state())
        with self.assertRaises(ServiceError) as e:self.start(ask_thread_id=ids['ask_thread_id'],authorize=True)
        self.assertEqual(e.exception.code,'ask_busy');self.client.pause.set();self.finish(ids,done)
    def test_disconnect_unknown_usage_no_retry_and_recovery_does_not_send(self):
        self.client.error=ServiceError('model_timeout_unknown','结果未知')
        ids,_,done=self.start(authorize=True);t=self.finish(ids,done);self.assertEqual(t['answer_status'],'partial')
        usage=self.s.ask.models.usage()['today'];self.assertEqual(usage['unknown_usage_requests'],1);self.assertIsNone(usage['known_total_tokens'])
        self.s.ask.recover();self.s.ask.detail(ids['ask_thread_id']);self.assertEqual(len(self.client.calls),1)
    def test_question_names_disambiguate_before_any_send(self):
        self.add('duplicate','方案',sender='another')
        with self.assertRaises(ServiceError) as e:self.preview('小王的方案')
        self.assertEqual(e.exception.code,'ask_person_ambiguous');self.assertEqual(self.client.calls,[])
    def test_Ask_config_action_cancels_active_request_and_preserves_draft(self):
        self.client.pause=threading.Event();ids,_,done=self.start(authorize=True)
        self.assertTrue(self.client.entered.wait(2))
        self.s.dispatch('ask.model.enable',{'enabled':False});self.client.pause.set();t=self.finish(ids,done)
        self.assertEqual(t['error_code'],'ask_stopped');self.assertEqual(t['run_status'],'stopped')
    def test_account_switch_does_not_expose_previous_thread(self):
        ids,_,done=self.start(authorize=True);self.finish(ids,done)
        with self.s.store.database() as db:
            source=self.s.store.settings()['source'];source['account_id']='another';self.s.store._set(db,'source',source)
        self.assertEqual(self.s.ask.list(),[])
        with self.assertRaises(ServiceError):self.s.ask.detail(ids['ask_thread_id'])
    def test_usage_does_not_combine_different_service_versions(self):
        ids,_,done=self.start(authorize=True);self.finish(ids,done)
        self.s.ask.models.save(base_url='https://native.invalid',model='native',api_key='new-key',protocol='gemini')
        ids,_,done=self.start(authorize=True);self.finish(ids,done)
        usage=self.s.ask.models.usage()['today'];self.assertEqual(usage['requests'],2);self.assertIsNone(usage['known_total_tokens']);self.assertEqual(len(usage['services']),2)
        self.assertEqual([g['known_total_tokens'] for g in usage['services']],[25,25])
    def test_shutdown_recovery_preserves_draft_no_replay(self):
        with self.s.store.database() as db:
            db.execute("INSERT INTO ask_threads VALUES('t','owned','旧问题','active','now','now')")
            db.execute("INSERT INTO ask_turns(turn_id,ask_thread_id,question,conclusion,answer_status,created_at) VALUES('turn','t','问题','部分文字','generating','now')")
            db.execute("INSERT INTO ask_runs(run_id,turn_id,scope_json,config_revision,coverage_json,evidence_json,status) VALUES('r','turn',?,1,'{}','[]','running')",(json.dumps(self.scope),))
        self.s.ask.recover();t=self.s.ask.detail('t')['turns'][0];self.assertEqual(t['conclusion'],'部分文字');self.assertEqual(t['run_status'],'interrupted');self.assertEqual(self.client.calls,[])
    def test_v11_migration_keeps_old_data_and_idempotent(self):
        with self.s.store.database() as db:
            for name in ('ask_citations','ask_runs','ask_turns','ask_threads','ask_grants','ask_fts','ask_index'):db.execute('DROP TABLE '+name)
            db.execute('PRAGMA user_version=11')
        before=self.tables()
        Store(self.root.name);Store(self.root.name)
        self.assertEqual(before,self.tables())
        with self.s.store.database() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],12)


class Response(io.BytesIO):
    def __init__(self,raw,media='text/event-stream'):super().__init__(raw);self.headers={'Content-Type':media}


class ProviderTests(unittest.TestCase):
    def run_provider(self,events,native=False,media='text/event-stream'):
        requests=[]
        class Opener:
            def open(self,request,timeout):requests.append(request);return Response(events,media)
        provider=AskProvider(opener=Opener())
        result=list(provider.stream({'protocol':'gemini' if native else 'openai','base_url':'https://owned.invalid/v1beta' if native else 'https://owned.invalid/v1','model':'owned'},'owned-secret','资料',StreamControl()))
        return result,requests
    def event(self,data):return ('data: '+json.dumps(data,ensure_ascii=False)+'\n\n').encode()
    def test_openai_text_usage_done(self):
        raw=self.event({'choices':[{'delta':{'content':'你好'},'finish_reason':None}]})+self.event({'choices':[{'delta':{},'finish_reason':'stop'}]})+self.event({'choices':[],'usage':{'total_tokens':15}})+b'data: [DONE]\n\n'
        out,req=self.run_provider(raw);self.assertEqual(''.join(c.get('text','') for c in out),'你好');self.assertIsNone(out[-1]['usage']['input_tokens']);self.assertEqual(out[-1]['usage']['total_tokens'],15)
        self.assertEqual(req[0].full_url,'https://owned.invalid/v1/chat/completions');self.assertEqual(req[0].get_header('Authorization'),'Bearer owned-secret')
    def test_gemini_excludes_hidden_thinking(self):
        raw=self.event({'candidates':[{'content':{'parts':[{'text':'隐藏推理','thought':True},{'text':'事实'}]}}]})+self.event({'candidates':[{'finishReason':'STOP'}],'usageMetadata':{'totalTokenCount':12}})
        out,req=self.run_provider(raw,True);self.assertEqual(''.join(c.get('text','') for c in out),'事实');self.assertIn(':streamGenerateContent?alt=sse',req[0].full_url);self.assertNotIn('owned-secret',req[0].full_url)
    def test_json_fallback_shows_once(self):
        out,_=self.run_provider(json.dumps({'choices':[{'message':{'content':'一次显示'},'finish_reason':'stop'}]}).encode(),media='application/json')
        self.assertEqual(len(out),2);self.assertTrue(out[-1]['finished'])
    def test_early_eof_and_truncated_finish_are_errors(self):
        for reason in (None,'length'):
            with self.assertRaises(ServiceError):self.run_provider(self.event({'choices':[{'delta':{'content':'部分'},'finish_reason':reason}]}))
    def test_split_secret_never_enters_delta(self):
        raw=self.event({'choices':[{'delta':{'content':'反射owned-'},'finish_reason':None}]})+self.event({'choices':[{'delta':{'content':'secret'},'finish_reason':'stop'}]})+b'data: [DONE]\n\n'
        out,_=self.run_provider(raw);self.assertNotIn('owned-secret',json.dumps(out));self.assertIn('[访问密钥]',''.join(c.get('text','') for c in out))
    def test_address_credentials_and_redirect_paths_rejected(self):
        for url in ['http://owned.invalid/v1','https://key@owned.invalid','https://owned.invalid/v1?key=secret']:
            with self.assertRaises(ServiceError):openai_endpoint(url)
        self.assertEqual(openai_endpoint('https://owned.invalid/v1/chat/completions'),'https://owned.invalid/v1')
