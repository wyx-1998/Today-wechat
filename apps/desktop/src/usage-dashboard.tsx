import React, { useEffect, useRef, useState } from "react";
import { request, explainError } from "./bridge";
import { dateTime, type State } from "./types";
type Call = {call_id: string; purpose: string; started_at: string; finished_at: string | null; outcome: string; input_tokens: number | null; output_tokens: number | null; total_tokens: number | null};
type Calls = {items: Call[]; total: number; offset: number; has_more: boolean};
const purposes: Record<string,string> = {automatic_text: "自动文字整理", real_saved_text: "文字整理", reanalyse_saved_text: "重新核对", connection_test: "服务连接", synthetic_analysis: "历史请求", meeting_notes: "会议纪要", daily_news_extract: "日报选材", daily_news_compose: "日报汇总", automatic_text_relevance: "任务筛选", real_saved_text_relevance: "任务筛选", reanalyse_saved_text_relevance: "任务筛选", synthetic_analysis_relevance: "任务筛选"};
const outcomes: Record<string,string> = {success: "成功", failed: "失败", unknown: "结果未知", sending: "处理中"};
export function UsageDashboard({state}: {state: State | null}) {
  const [period, setPeriod] = useState<"today" | "month">("today");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<Calls | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const sequence = useRef(0);
  const usage = state?.model_usage[period];
  // Explicit refresh preserves stable pagination while background requests arrive.
  useEffect(() => {let alive=true; const current=++sequence.current; setLoading(true); setError(""); setData(null);
    request<Calls>("model.calls", {period, limit:25, offset:page*25}).then(result => {
      if (alive && current===sequence.current) {if (page && result.total<=page*25) setPage(Math.max(0,Math.ceil(result.total/25)-1)); else setData(result);}
    }).catch(error => {if(alive) setError(explainError(error));}).finally(() => {if(alive) setLoading(false);});
    return () => {alive=false;};
  }, [period, page, refresh]);
  return <section className="usage-dashboard"><div className="section-title"><div><h2>模型与用量</h2><p>实际请求记录 · 不设置产品侧上限</p></div><div className="segmented" aria-label="用量时间范围">{(["today","month"] as const).map(value => <button key={value} aria-pressed={period===value} onClick={() => {setPeriod(value); setPage(0);}}>{value==="today" ? "今天" : "本月"}</button>)}</div></div>
    <div className="usage-status"><span>请求 <strong>{usage?.requests ?? 0}</strong></span><span>成功 {usage?.success ?? 0}</span><span>失败 {usage?.failed ?? 0}</span><span>结果未知 {usage?.unknown ?? 0}</span><span>处理中 {usage?.sending ?? 0}</span></div>
    <div className="usage-metrics">{(["input", "output", "total"] as const).map(kind => <article key={kind}><h3>{({input:"输入",output:"输出",total:"总计"})[kind]} Token</h3><strong>{usage?.[`known_${kind}_tokens`] == null ? "未知" : usage[`known_${kind}_tokens`]?.toLocaleString("zh-CN")}</strong><p>{kind==="total" ? usage?.unknown_usage_requests ?? 0 : usage?.[`unknown_${kind}_requests`] ?? 0} 次未返回该项用量</p></article>)}</div>
    <p className="field-note">已返回的分项合计；有缺失时不代表完整用量。总计可能包含思考 Token，不用输入与输出相加替代；不推算费用。</p>
    <details className="recent-calls"><summary>最近请求 · {period==="today" ? "今天" : "本月"}</summary><div className="section-title"><p>只展示时间、用途、状态和用量，不展示发送内容或密钥。</p><button disabled={loading} onClick={() => setRefresh(value => value+1)}>刷新请求记录</button></div>
      {error && <p className="notice error" role="alert">{error}</p>}{loading && <p role="status">正在读取请求记录…</p>}{data && <><div className="directory-table-scroll"><table className="directory-table calls-table"><thead><tr><th>时间</th><th>用途</th><th>状态</th><th>输入</th><th>输出</th><th>总计 Token</th></tr></thead><tbody>{data.items.map(call => <tr key={call.call_id}><td>{dateTime(call.started_at)}</td><td>{purposes[call.purpose] || "文字处理"}</td><td>{outcomes[call.outcome] || "状态未知"}</td>{(["input","output","total"] as const).map(kind => <td key={kind}>{call[`${kind}_tokens`] == null ? "未知" : call[`${kind}_tokens`]?.toLocaleString("zh-CN")}</td>)}</tr>)}</tbody></table></div>{!data.total && <p className="empty-state">这个时间范围还没有请求记录。</p>}<div className="pager"><span>共 {data.total} 条 · {page+1}/{Math.max(1,Math.ceil(data.total/25))} 页</span><button disabled={loading || !page} onClick={() => setPage(page-1)}>上一页请求</button><button disabled={loading || !data.has_more} onClick={() => setPage(page+1)}>下一页请求</button></div></>}
    </details>
  </section>;
}
