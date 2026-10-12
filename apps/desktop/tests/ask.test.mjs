import test from 'node:test';
import assert from 'node:assert/strict';
import {AskSequence,askStatus} from '../src/ask-types.ts';
import {TauriAgent} from '../src/ask-agent.ts';
const ids={run_id:'r',ask_thread_id:'t',turn_id:'m'};
const events=[{type:'RUN_STARTED',threadId:'t',runId:'r'},{type:'STEP_STARTED',stepName:'查找消息'},
  {type:'STEP_FINISHED',stepName:'查找消息'},{type:'TEXT_MESSAGE_START',messageId:'m',role:'assistant'},
  {type:'TEXT_MESSAGE_CONTENT',messageId:'m',delta:'方案需要提交。[M1]'},{type:'TEXT_MESSAGE_END',messageId:'m'},
  {type:'CUSTOM',name:'ask.citations',value:[{message_id:'source'}]},{type:'STATE_SNAPSHOT',snapshot:{status:'answered'}},
  {type:'RUN_FINISHED',threadId:'t',runId:'r'}];

test('sequence ignores duplicates, rejects gaps and late packets from another run',()=>{
  const gate=new AskSequence();assert.equal(gate.accept('r',1),true);assert.equal(gate.accept('r',1),false);
  assert.throws(()=>gate.accept('r',3),/gap/);assert.throws(()=>gate.accept('other',2),/mismatch/);
});
test('locked SDK processes real event contract and retains text and citation state',async()=>{
  const transport={async start(params,receive){events.forEach((event,i)=>receive({run_id:'r',sequence:i+1,event}));return ids;},async stop(){throw Error('Unexpected stop');}};
  const agent=new TauriAgent({},()=>{},transport),seen=[];
  await agent.runAgent({}, {onEvent:({event})=>{seen.push(event.type);}});
  assert.equal(agent.messages[0].content,'方案需要提交。[M1]');assert.equal(agent.state.status,'answered');assert.equal(seen.length,events.length);
});
test('gap ends observation and cancels backend after late start acknowledgement, without replay',async()=>{
  let starts=0,stops=[];
  const agent=new TauriAgent({},()=>{}, {async start(params,receive){starts++;receive({run_id:'r',sequence:2,event:events[0]});return ids;},async stop(id){stops.push(id);}});
  await assert.rejects(agent.runAgent(),/流式连接已中断/);await new Promise(resolve=>setImmediate(resolve));assert.equal(starts,1);assert.deepEqual(stops,['r']);
});
test('cancellation before start acknowledgement stops the acknowledged run and ignores late chunks',async()=>{
  let acknowledge,receive;const stopped=[];
  const agent=new TauriAgent({},()=>{}, {start(params,callback){receive=callback;return new Promise(resolve=>{acknowledge=resolve;});},async stop(id){stopped.push(id);}});
  const promise=agent.runAgent();await new Promise(resolve=>setImmediate(resolve));agent.abortRun();acknowledge(ids);
  receive({run_id:'r',sequence:1,event:events[0]});await promise.catch(()=>{});await new Promise(resolve=>setImmediate(resolve));assert.deepEqual(stopped,['r']);assert.equal(agent.messages.length,0);
});
test('stopped or partial answers never display as completed',()=>{assert.match(askStatus('answered','ask_stopped'),/未完成/);assert.match(askStatus('partial'),/需核对/);});

test('packaged Python events are accepted by the pinned AG-UI schema',async()=>{
  const {readFileSync,existsSync}=await import('node:fs');const file=new URL('../../../output/ask-events.json',import.meta.url);
  if(!existsSync(file))return;
  const packets=JSON.parse(readFileSync(file,'utf8'));
  const agent=new TauriAgent({},()=>{},{async start(_params,receive){packets.forEach(receive);return ids;},async stop(){throw Error('Unexpected stop');}});
  await agent.runAgent();assert.equal(agent.state.status,'no_evidence');assert.ok(agent.messages[0].content.includes('没有找到足够依据'));
});
