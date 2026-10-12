import { AbstractAgent, type AgentSubscriber, type AgentStateMutation } from '@ag-ui/client';
import type { BaseEvent, RunAgentInput } from '@ag-ui/core';
import { EventSchema } from '@ag-ui/core/schemas';
import { Observable, throwError } from 'rxjs';
import { Channel, invoke } from '@tauri-apps/api/core';
import { request } from './bridge.ts';
import { AskSequence, type AskStarted } from './ask-types.ts';

type Packet = {run_id: string; sequence: number; event: unknown; transport_error?: boolean};
export type AskTransport = {start: (params: Record<string,unknown>, receive: (packet:Packet)=>void)=>Promise<AskStarted>; stop: (id:string)=>Promise<unknown>};
const desktopTransport:AskTransport = {
  start(params,receive) {const channel=new Channel<Packet>();channel.onmessage=receive;return invoke<AskStarted>('ask_stream',{params,onEvent:channel});},
  stop(id) {return request('ask.stop',{run_id:id});},
};
export class TauriAgent extends AbstractAgent {
  private params: Record<string,unknown>;
  private started: (ids:AskStarted)=>void;
  private transport: AskTransport;
  private cancelStream: (()=>void) | null = null;
  constructor(params: Record<string, unknown>, started: (ids: AskStarted) => void, transport=desktopTransport) {
    super({debug:false});this.params=params;this.started=started;this.transport=transport;
  }
  abortRun():void {this.cancelStream?.();super.abortRun();}
  run(_input: RunAgentInput): Observable<BaseEvent> {
    return new Observable(subscriber => {
      const gate = new AskSequence();
      let terminal = false, detached = false, ids: AskStarted | null = null;
      this.cancelStream=()=>subscriber.error(new Error('回答已停止；已生成片段保留。'));
      void this.transport.start(this.params, packet=>{
        if(detached || terminal)return;
        try {
          if(packet.transport_error)throw new Error('ask_transport_closed');
          if(!gate.accept(packet.run_id,packet.sequence))return;
          const event=EventSchema.parse(packet.event) as BaseEvent;
          terminal=event.type==='RUN_FINISHED' || event.type==='RUN_ERROR';
          subscriber.next(event);if(terminal)subscriber.complete();
        } catch {subscriber.error(new Error('流式连接已中断；请刷新已保存回答，不会自动重发。'));}
      }).then(value=>{
        ids=value;
        if(detached && !terminal)void this.transport.stop(value.run_id).catch(()=>{});
        else if(!detached)this.started(value);
      }).catch(error=>{if(!detached)subscriber.error(error);});
      return ()=>{detached=true;this.cancelStream=null;if(!terminal && ids)void this.transport.stop(ids.run_id).catch(()=>{});};
    });
  }
  protected onError(_input:RunAgentInput,error:Error,_subscribers:AgentSubscriber[]):Observable<AgentStateMutation> {return throwError(()=>error);}
}
