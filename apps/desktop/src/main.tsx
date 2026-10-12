import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { request, explainError } from "./bridge";
import { TAG_NAME_LIMIT, validateTagName } from "./tag-name";
import { ChatDirectory } from "./chat-directory";
import { TagPicker } from "./tag-picker";
import { AskPanel } from "./ask-panel";
import { ModelPanel } from "./model-panel";
import { Results } from "./results";
import { MessageBrowser } from "./message-browser";
import { MeetingPanel } from "./meeting-panel";
import {
  Chat,
  Tag,
  State,
  Report,
  chatName,
  dateTime,
  reportText,
} from "./types";
import "./style.css";

type Page = "today" | "tasks" | "ask" | "settings";
type Tab = "connection" | "chats" | "messages" | "model";
type Confirmation = {
  title: string;
  body: string;
  action: string;
  proceed: () => void;
};

function Icon({ name }: { name: string }) {
  const paths: Record<string, React.ReactNode> = {
    today: (
      <>
        <path d="M8 3v4 M16 3v4 M4 10h16 M6 5h12a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z" />
      </>
    ),
    tasks: (
      <>
        <path d="M9 6h11 M9 12h11 M9 18h11 M3 6l1 1 2-2 M3 12l1 1 2-2 M3 18l1 1 2-2" />
      </>
    ),
    ask: <path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-5 3v-3H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z M7 11h.1 M12 11h.1 M17 11h.1" />,
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="m9 3-1 3-3 1v3l-2 2 2 2v3l3 1 1 3h6l1-3 3-1v-3l2-2-2-2V7l-3-1-1-3Z" />
      </>
    ),
    sync: (
      <>
        <path d="M20 7a9 9 0 0 0-15-2L3 7m0-4v4h4m-3 10a9 9 0 0 0 15 2l2-2m0 4v-4h-4" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    message: (
      <path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z" />
    ),
  };
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {paths[name] || paths.message}
    </svg>
  );
}

function ConfirmDialog({
  value,
  close,
}: {
  value: Confirmation | null;
  close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (value && !dialog.current?.open) dialog.current?.showModal();
    else if (!value) dialog.current?.close();
  }, [value]);
  return (
    <dialog ref={dialog} onCancel={close} aria-labelledby="confirm-title">
      <h2 id="confirm-title">{value?.title}</h2>
      <p>{value?.body}</p>
      <div className="actions">
        <button autoFocus onClick={close}>
          保留并返回
        </button>
        <button
          className="primary"
          onClick={() => {
            const proceed = value?.proceed;
            close();
            proceed?.();
          }}
        >
          {value?.action}
        </button>
      </div>
    </dialog>
  );
}

function App() {
  const [state, setState] = useState<State | null>(null);
  const [page, setPage] = useState<Page>("today");
  const [tab, setTab] = useState<Tab>("connection");
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(
    null,
  );
  const [pending, setPending] = useState<string[]>([]);
  const pendingRef = useRef(new Set<string>());
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [draft, setDraft] = useState<Chat | null>(null);
  const chatDialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {if (draft) chatDialog.current?.showModal(); else chatDialog.current?.close();}, [draft]);
  const [modelDirty, setModelDirty] = useState(false);
  const [meetingOpen, setMeetingOpen] = useState(false);
  const [meetingDirty, setMeetingDirty] = useState(false);
  const [meetingBusy, setMeetingBusy] = useState(false);
  const [meetingRefresh, setMeetingRefresh] = useState(0);
  const [chatBusy, setChatBusy] = useState(false);
  const [modelReset, setModelReset] = useState(0);
  const [tagName, setTagName] = useState("");
  const [tagEdit, setTagEdit] = useState<Tag | null>(null);
  const [tagComposing, setTagComposing] = useState(false);
  const tagValidation = validateTagName(tagEdit ? tagEdit.name : tagName);
  const tagTooLong = !tagComposing && tagValidation.length > TAG_NAME_LIMIT;
  useEffect(() => setTagComposing(false), [page, tab, tagEdit?.tag_id]);
  const [root, setRoot] = useState("");
  const [selfId, setSelfId] = useState("");
  const [authorize, setAuthorize] = useState(false);
  const [encryptedRoot, setEncryptedRoot] = useState("");
  const [keysFile, setKeysFile] = useState("");
  const [encryptedAuthorize, setEncryptedAuthorize] = useState(false);
  const [range, setRange] = useState("");

  const dirty =
    modelDirty || chatBusy || meetingDirty || meetingBusy ||
    !!draft ||
    !!tagEdit ||
    !!tagName ||
    !!root ||
    !!selfId ||
    authorize ||
    !!encryptedRoot || !!keysFile || encryptedAuthorize ||
    !!range;
  const quitHandler = useRef<() => void>(() => {});
  const busy = pending.length > 0 || chatBusy;
  const connected = !!state?.source;
  const syncing = !!state?.busy || pending.includes("sync.now");

  async function refresh() {
    const next = await request<State>("state");
    setState(next);
    return next;
  }
  async function run<T>(
    command: string,
    params: Record<string, unknown> = {},
    text?: string,
  ): Promise<T | undefined> {
    if (pendingRef.current.has(command)) return;
    pendingRef.current.add(command);
    setPending([...pendingRef.current]);
    setNotice(null);
    try {
      const result = await request<T>(command, params);
      await refresh();
      if (text) setNotice({ text, error: false });
      return result;
    } catch (error) {
      setNotice({ text: explainError(error), error: true });
    } finally {
      pendingRef.current.delete(command);
      setPending([...pendingRef.current]);
    }
  }
  function discard() {
    setMeetingOpen(false); setMeetingDirty(false);
    setModelDirty(false);
    setModelReset(value => value + 1);
    setDraft(null);
    setTagEdit(null);
    setTagName("");
    setRoot("");
    setSelfId("");
    setAuthorize(false);
    setEncryptedRoot("");
    setKeysFile("");
    setEncryptedAuthorize(false);
    setRange("");
  }
  function leave(action: () => void) {
    if (meetingBusy) {setNotice({text: "正在处理纪要，请完成后再离开。", error: false}); return;}
    if (chatBusy) {setNotice({text: "正在保存会话配置，请完成后再离开。", error: false}); return;}
    if (!dirty) action();
    else
      setConfirmation({
        title: "放弃未保存的修改？",
        body: "未保存的修改会丢弃，已保存的数据保留。",
        action: "放弃修改并继续",
        proceed: () => {
          discard();
          action();
        },
      });
  }
  quitHandler.current = () => {
    if (chatBusy) {setNotice({text: "正在保存会话配置，请完成后再退出。", error: false}); return;}
    if (dirty)
      setConfirmation({
        title: "放弃修改并退出？",
        body: "未保存的配置不会写入。退出后停止同步，已保存的数据与成功进度保留。",
        action: "放弃修改并退出",
        proceed: () => {
          void invoke("finish_quit");
        },
      });
    else void invoke("finish_quit");
  };
  useEffect(() => {
    void invoke("set_dirty", { dirty }).catch(() => {});
  }, [dirty]);
  useEffect(() => {
    let alive = true,
      polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const next = await request<State>("state");
        if (alive) setState(next);
      } catch (error) {
        if (alive) setNotice({ text: explainError(error), error: true });
      } finally {
        polling = false;
      }
    };
    void poll();
    const timer = setInterval(poll, 1500);
    const unsubscribe = listen("request-quit", () => quitHandler.current());
    return () => {
      alive = false;
      clearInterval(timer);
      void unsubscribe.then((fn) => fn());
    };
  }, []);
  useEffect(() => {setDraft(null);}, [state?.source?.account_id]);

  async function sync() {
    const report = await run<Report>("sync.now");
    if (report)
      setNotice({
        text: reportText(report),
        error: report.outcome === "partial",
      });
  }

  function viewSources() {
    leave(() => {
      setPage("settings");
      setTab("messages");
    });
  }
  async function saveChat() {
    if (!draft) return;
    const result = await run<State>(
      "chat.save",
      {
        chat_id: draft.chat_id,
        display_name: draft.display_name,
        sync_enabled: draft.sync_enabled,
        analysis_enabled: draft.analysis_enabled,
        tag_ids: draft.tags.map((tag) => tag.tag_id),
      },
      "会话配置已保存。",
    );
    if (result) setDraft(null);
  }
  async function saveTag() {
    if (tagComposing || !tagValidation.valid) return;
    const result = await run<{ tag_id: string }>(
      "tag.save",
      {
        name: tagEdit ? tagEdit.name : tagName,
        ...(tagEdit ? { tag_id: tagEdit.tag_id } : {}),
      },
      "标签已保存；不会改变会话读取范围。",
    );
    if (result) {
      setTagEdit(null);
      setTagName("");
    }
  }
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到内容
      </a>
      <aside className="sidebar">
        <div className="wordmark">
          wechat<span>today</span>
        </div>
        <nav aria-label="主导航">
          {(
            [
              ["today", "今天"],
              ["tasks", "待办"],
              ["ask", "问我"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              aria-current={page === id ? "page" : undefined}
              className={page === id ? "nav-item active" : "nav-item"}
              onClick={() => leave(() => setPage(id))}
            >
              <Icon name={id} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-footer">
        <div className="sidebar-bottom">
          <span
            className={
              "status-dot " +
              (state?.paused ? "paused" : connected ? "ready" : "")
            }
          />
          {state?.paused
            ? "同步已暂停"
            : syncing
              ? "正在同步"
              : connected
                ? "本地来源已连接"
                : "尚未连接来源"}
          <small>
            本地同步版 · 0.1.0
            <br />
            {state?.text_model?.enabled ? state.automatic_text?.grant?.status === "active" ? "文字自动整理已授权" : "文本模型已启用 · 发送需单独确认" : "文本模型未启用"}
          </small>
        </div>
        <button
          className={"settings-button" + (page === "settings" ? " active" : "")}
          aria-label="设置" title="设置" aria-current={page === "settings" ? "page" : undefined}
          onClick={() => leave(() => setPage("settings"))}
        >
          <Icon name="settings" /><span>设置</span>
        </button>
        </div>
      </aside>
      <main id="main-content" className="workspace">
        <header className="topbar">
          <div>
            <h1>
              {page === "today" ? "今天" : page === "tasks" ? "待办" : page === "ask" ? "问我" : "设置"}
            </h1>
            <span>
              {page === "today"
                ? new Date().toLocaleDateString("zh-CN", {
                    month: "long",
                    day: "numeric",
                    weekday: "long",
                  })
                : page === "tasks"
                  ? "从聊天中发现需要我做的事"
                  : page === "ask" ? "" : "本地来源、会话范围与原文"}
            </span>
          </div>
          <div className="actions" hidden={page === "ask"}>
            {page === "today" && <button disabled={!state || busy || meetingBusy} onClick={() => leave(() => setMeetingOpen(true))}>导入纪要</button>}
            <span className="mode-badge">
              {connected
                  ? state?.source?.mode === "encrypted" ? "本地加密来源" : "本地只读来源"
                  : "未连接"}
            </span>
            <button
              className="icon-text"
              disabled={!connected || !!state?.paused || syncing || busy}
              onClick={() => {
                void sync();
              }}
            >
              <Icon name="sync" />
              {syncing ? "同步中…" : "立即同步"}
            </button>
          </div>
        </header>
        {notice && (
          <div
            className={"notice " + (notice.error ? "error" : "")}
            role={notice.error ? "alert" : "status"}
          >
            <span>{notice.text}</span>
            <button aria-label="关闭提示" onClick={() => setNotice(null)}>
              <Icon name="close" />
            </button>
          </div>
        )}
        {!state && page !== "ask" && (
          <div className="loading-state">
            正在连接本地读取程序…
            <button
              onClick={() => {
                void refresh().catch((error) =>
                  setNotice({ text: explainError(error), error: true }),
                );
              }}
            >
              重新检查
            </button>
          </div>
        )}
        {(page === "today" || page === "tasks") && <Results page={page} state={state} refreshToken={meetingRefresh} configure={() => leave(() => { setPage("settings"); setTab("model"); })}/>}
        <section className="ask-host" hidden={page !== "ask"}><AskPanel state={state} configure={() => leave(() => {setPage("settings");setTab("model");})}/></section>
        <section className="settings-area" hidden={page !== "settings"}>
          <nav className="settings-nav" aria-label="设置栏目">
            {(
              [
                ["connection", "本地同步"],
                ["chats", "会话与标签"],
                ["messages", "已同步消息"],
                ["model", "模型与用量"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                className={tab === id ? "selected" : ""}
                aria-current={tab === id ? "page" : undefined}
                onClick={() => leave(() => setTab(id))}
              >
                {label}
              </button>
            ))}
          </nav>
          <div hidden={tab !== "model"} className="model-container"><ModelPanel state={state} refresh={refresh} onDirty={setModelDirty} resetToken={modelReset}/></div>
          <div className="content-scroll" hidden={tab !== "connection"}>
            <div className="section-title">
              <h2>连接本地来源</h2>
              <span>只读 · 不发送云端</span>
            </div>
            {connected && (
              <div className="sync-summary">
                <div className="section-title">
                  <h3>
                    {state?.paused
                      ? "同步已暂停"
                      : syncing
                        ? "正在读取并保存"
                        : "同步状态"}
                  </h3>
                  <button
                    disabled={
                      pending.includes("sync.pause") ||
                      pending.includes("sync.resume")
                    }
                    onClick={() => {
                      void run<State>(
                        state?.paused ? "sync.resume" : "sync.pause",
                        {},
                        state?.paused
                          ? "已恢复同步，下一轮从成功进度继续。"
                          : "已暂停同步，已保存数据和进度保留。",
                      );
                    }}
                  >
                    {state?.paused ? "恢复同步" : "暂停同步"}
                  </button>
                </div>
                <dl>
                  <dt>已保存消息</dt>
                  <dd>{state?.message_count} 条</dd>
                  <dt>同步起点</dt>
                  <dd>{dateTime(state?.time_start)}</dd>
                  <dt>最近完成</dt>
                  <dd>{dateTime(state?.last_sync?.finished_at)}</dd>
                  <dt>检查节奏</dt>
                  <dd>应用运行期间每分钟一次，退出后停止</dd>
                </dl>
                {state?.last_sync && (
                  <p
                    className={
                      "report-line " +
                      (state.last_sync.outcome === "partial" ? "warning" : "")
                    }
                  >
                    {reportText(state.last_sync)}
                  </p>
                )}
                {state?.last_sync?.errors.map((error) => (
                  <div className="source-error" key={error.chat_id}>
                    <strong>
                      {state.chats.find((c) => c.chat_id === error.chat_id)
                        ?.chat_name || (error.chat_id === "__source__" ? "来源准备失败" : "读取失败的会话")}
                    </strong>
                    <p>{error.message}</p>
                    <small>{error.code}</small>
                  </div>
                ))}
                <div className="actions">
                  <button onClick={() => leave(() => setTab("chats"))}>
                    配置会话与标签
                  </button>
                  <button onClick={viewSources}>查看已同步消息</button>
                </div>
                <details className="advanced">
                  <summary>调整读取起点</summary>
                  <p>缩小范围不会删除已保存历史；扩大范围会重新扫描并去重。</p>
                  <label>
                    开始时间
                    <input
                      type="datetime-local"
                      value={range}
                      onInput={(event) => setRange(event.currentTarget.value)}
                    />
                  </label>
                  <button
                    disabled={!range || busy || syncing}
                    onClick={async () => {
                      const result = await run<State>(
                        "range.save",
                        { time_start: new Date(range).toISOString() },
                        "读取起点已保存，历史数据保留。",
                      );
                      if (result) setRange("");
                    }}
                  >
                    保存读取起点
                  </button>
                </details>
                <details className="advanced">
                  <summary>本地数据位置</summary>
                  <p className="path-text">{state?.source?.root}</p>
                  <p>这是来源目录，不是应用数据库位置；消息副本由应用保存。</p>
                  {state?.source?.mode === "encrypted" && <>
                    <p className="path-text">密钥文件：{state.source.keys_file}</p>
                    <p>只保存文件路径，不复制原始密钥；每轮临时解密，处理后删除副本。微信版本变化可能影响读取。</p>
                  </>}
                </details>
              </div>
            )}
            <details className="advanced connection-form">
              <summary>连接加密目录与已有密钥文件</summary>
              <p>
                全程本地，不提取密钥、不提权、不上传。发现会话会读取联系人和会话信息。
                同步时消息分库可能含未选会话，需先临时解密，再只保存你开启的会话及时间范围；临时副本处理后删除。
                当前支持 message_N.db 与 biz_message_N.db 正文分库；不读取其他名称的数据库、全文索引或媒体内容。微信改变分库格式时需更新读取程序。
              </p>
              <p>支持已有工具的 JSON 格式：相对数据库路径对应 enc_key。原始密钥不会显示或写入应用配置；微信版本变化可能影响读取。</p>
              <label>
                加密数据库目录
                <div className="input-row">
                  <input value={encryptedRoot} onInput={(event) => {
                    setEncryptedRoot(event.currentTarget.value); setEncryptedAuthorize(false);
                  }} placeholder="含 message 子目录的数据库目录" />
                  <button disabled={busy || syncing} onClick={async () => {
                    try {
                      const selected = await invoke<string | null>("pick_source", { encrypted: true });
                      if (selected) { setEncryptedRoot(selected); setEncryptedAuthorize(false); }
                    } catch (error) { setNotice({ text: explainError(error), error: true }); }
                  }}>选择加密目录</button>
                </div>
              </label>
              <label>
                已有 JSON 密钥文件
                <div className="input-row">
                  <input value={keysFile} onInput={(event) => {
                    setKeysFile(event.currentTarget.value); setEncryptedAuthorize(false);
                  }} placeholder="例如：私有工具生成的 all_keys.json" />
                  <button disabled={busy || syncing} onClick={async () => {
                    try {
                      const selected = await invoke<string | null>("pick_keys_file");
                      if (selected) { setKeysFile(selected); setEncryptedAuthorize(false); }
                    } catch (error) { setNotice({ text: explainError(error), error: true }); }
                  }}>选择密钥文件</button>
                </div>
              </label>
              <label>
                本人账号标识（可选）
                <input value={selfId} onInput={(event) => setSelfId(event.currentTarget.value)} placeholder="未填写时，本人身份保留未知" />
              </label>
              <label className="check-label">
                <input type="checkbox" checked={encryptedAuthorize} onChange={(event) => setEncryptedAuthorize(event.target.checked)} />
                我确认本地读取所选目录及密钥，了解临时解密范围；不向云端发送
              </label>
              <button disabled={!encryptedRoot || !keysFile || !encryptedAuthorize || busy || syncing} onClick={async () => {
                const result = await run<State>("source.encrypted", {
                  root: encryptedRoot, keys_file: keysFile, authorize: encryptedAuthorize, self_id: selfId || null,
                }, "已在本地发现会话，请选择需要同步的范围；尚未同步消息。");
                if (result) { discard(); setTab("chats"); }
              }}>{pending.includes("source.encrypted") ? "正在准备会话信息…" : "确认并发现加密来源会话"}</button>
            </details>
            <details className="advanced connection-form">
              <summary>连接自己的已解密目录</summary>
              <p>
                此入口不执行解密。请选择含 message
                子目录的已解密数据；只有确认后才会发现会话，发现后仍需你选择同步范围。
              </p>
              <label>
                已解密目录
                <div className="input-row">
                  <input
                    value={root}
                    onInput={(event) => {
                      setRoot(event.currentTarget.value);
                      setAuthorize(false);
                    }}
                    placeholder="请选择本地目录"
                  />
                  <button
                    disabled={busy || syncing}
                    onClick={async () => {
                      try {
                        const selected = await invoke<string | null>(
                          "pick_source",
                        );
                        if (selected) {
                          setRoot(selected);
                          setAuthorize(false);
                        }
                      } catch (error) {
                        setNotice({ text: explainError(error), error: true });
                      }
                    }}
                  >
                    选择目录
                  </button>
                </div>
              </label>
              <label>
                本人账号标识（可选）
                <input
                  value={selfId}
                  onInput={(event) => setSelfId(event.currentTarget.value)}
                  placeholder="未填写时，本人身份保留未知"
                />
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={authorize}
                  onChange={(event) => setAuthorize(event.target.checked)}
                />
                我确认只读访问所选目录，不向云端发送内容
              </label>
              <button
                disabled={!root || !authorize || busy || syncing}
                onClick={async () => {
                  const result = await run<State>(
                    "source.local",
                    { root, authorize, self_id: selfId || null },
                    "已连接所选本地来源，请选择会话。",
                  );
                  if (result) {
                    discard();
                    setTab("chats");
                  }
                }}
              >
                确认并发现会话
              </button>
            </details>
          </div>
          <div className="content-scroll settings-chat-panel" hidden={tab !== "chats"}>
            <div className="section-title">
              <div>
                <h2>会话与标签</h2>
                <p>分别配置本地同步与文字分析；保存不代表云端授权。</p>
              </div>
              <button
                disabled={!connected || busy || syncing}
                onClick={() => {
                  void run<State>(
                    "source.refresh",
                    {},
                    "会话列表已检查，新会话默认未选。",
                  );
                }}
              >
                刷新会话
              </button>
            </div>
            {!connected ? (
              <div className="empty-state">
                <h3>先连接本地来源</h3>
                <button onClick={() => setTab("connection")}>
                  前往本地同步
                </button>
              </div>
            ) : (
              <>
              <ChatDirectory state={state} busy={busy} refresh={refresh} navigate={leave} resetToken={modelReset}
                onBusy={setChatBusy} onEdit={chat => leave(() => setDraft({...chat, tags: [...chat.tags]}))}
                onRename={tag => leave(() => setTagEdit({...tag}))}
                onDelete={tag => setConfirmation({title: `删除标签“${tag.name}”？`,
                  body: "会话上的该标签关联也会移除；不会删除消息或会话，不改变同步及分析开关。",
                  action: "删除此标签", proceed: () => {void run("tag.delete", {tag_id: tag.tag_id}, "标签及关联已删除，消息和读取范围保留。");}})}
                editor={<dialog ref={chatDialog} className="chat-config-drawer" aria-label="会话配置"
                  onCancel={event => {event.preventDefault(); if (!busy) leave(() => setDraft(null));}}>{draft && (
              <div className="editor">
                <div className="section-title">
                  <h3>配置 · {draft.chat_name}</h3>
                  <button
                    autoFocus disabled={busy}
                    onClick={() => leave(() => setDraft(null))}
                  >
                    关闭配置
                  </button>
                </div>
                <label>
                  显示名称
                  <input
                    value={draft.display_name || ""}
                    maxLength={100}
                    onInput={(event) =>
                      setDraft({
                        ...draft,
                        display_name: event.currentTarget.value,
                      })
                    }
                    placeholder={draft.chat_name}
                  />
                </label>
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={draft.sync_enabled}
                    onChange={(event) =>
                      setDraft({ ...draft, sync_enabled: event.target.checked,
                        analysis_enabled: event.target.checked && draft.analysis_enabled })
                    }
                  />
                  开启本地同步
                </label>
                <label className="check-label">
                  <input type="checkbox" checked={draft.analysis_enabled} disabled={!draft.sync_enabled}
                    onChange={event => setDraft({...draft, analysis_enabled: event.currentTarget.checked})}/>
                  纳入文字自动分析
                </label>
                <p className="field-note">
                  开启本地同步不会授权云端发送。文字整理的发送范围在“模型与用量”中另行确认。
                </p>
                <TagPicker tags={state?.tags || []} selected={draft.tags.map(tag => tag.tag_id)}
                  onChange={ids => setDraft({...draft, tags: (state?.tags || []).filter(tag => ids.includes(tag.tag_id))})} disabled={busy}/>
                <div className="actions">
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => {
                      void saveChat();
                    }}
                  >
                    {pending.includes("chat.save") ? "保存中…" : "保存会话配置"}
                  </button>
                  <button
                    disabled={busy}
                    onClick={() => leave(() => setDraft(null))}
                  >
                    放弃修改
                  </button>
                </div>
              </div>
             )}</dialog>}
                tagForm={            <section className="tag-manager">
              <h2>管理标签</h2>
              <div className="input-row">
                <label className="grow">
                  {tagEdit ? "修改标签名" : "新标签名称"}
                  <input
                    aria-describedby="tag-name-help"
                    aria-invalid={tagTooLong || undefined}
                    value={tagEdit ? tagEdit.name : tagName}
                    onCompositionStart={() => setTagComposing(true)}
                    onCompositionEnd={() => setTagComposing(false)}
                    onInput={(event) =>
                      tagEdit
                        ? setTagEdit({
                            ...tagEdit,
                            name: event.currentTarget.value,
                          })
                        : setTagName(event.currentTarget.value)
                    }
                    placeholder="例如：工作、家人"
                  />
                </label>
                <button
                  disabled={
                    busy || tagComposing || !tagValidation.valid
                  }
                  onClick={() => {
                    void saveTag();
                  }}
                >
                  {tagEdit ? "保存标签名" : "新建标签"}
                </button>
                {tagEdit && (
                  <button onClick={() => leave(() => setTagEdit(null))}>
                    放弃修改
                  </button>
                )}
              </div>
              <div
                id="tag-name-help"
                className={`tag-name-help${tagTooLong ? " invalid" : ""}`}
                aria-live="polite"
                aria-atomic="true"
              >
                <span>
                  {tagTooLong
                    ? `超出 ${tagValidation.length - TAG_NAME_LIMIT} 个字符，请缩短后保存。输入尚未保存，也没有被截断。`
                    : `标签名需为 1–${TAG_NAME_LIMIT} 个字符；不计首尾空格。`}
                </span>
                <span className="tag-name-count">
                  {tagValidation.length} / {TAG_NAME_LIMIT}
                </span>
              </div>

            </section>}/>
              </>
            )}
          </div>
          {page === "settings" && tab === "messages" && <MessageBrowser state={state} onChooseChats={() => leave(() => setTab("chats"))}/> }
        </section>
      </main>
      {meetingOpen && state && <MeetingPanel state={state} close={() => leave(() => setMeetingOpen(false))}
        onDirty={setMeetingDirty} onBusy={setMeetingBusy}
        configure={() => leave(() => {setMeetingOpen(false); setPage("settings"); setTab("model");})}
        published={async () => {await refresh(); setMeetingRefresh(value => value + 1);}}/>}
      <ConfirmDialog value={confirmation} close={() => setConfirmation(null)} />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
