#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use serde_json::{json, Value};
use std::os::unix::process::CommandExt;
use std::{
    collections::HashMap,
    io::{BufRead, BufReader, Write},
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};
use tauri::{ipc::Channel, Emitter, Manager};
use tauri_plugin_dialog::DialogExt;
use tokio::sync::oneshot;

#[derive(Clone, Debug, Serialize)]
struct BridgeError {
    code: String,
    message: String,
    retryable: bool,
}
fn error(code: &str, message: &str) -> BridgeError {
    BridgeError {
        code: code.into(),
        message: message.into(),
        retryable: true,
    }
}
type Reply = Result<Value, BridgeError>;

struct Worker {
    child: Mutex<Child>,
    input: Mutex<Option<ChildStdin>>,
    pending: Mutex<HashMap<String, oneshot::Sender<Reply>>>,
    streams: Mutex<HashMap<String, Channel<Value>>>,
    counter: AtomicU64,
    closing: AtomicBool,
    dirty: AtomicBool,
}

impl Worker {
    fn launch(app: &tauri::AppHandle) -> Result<Arc<Self>, Box<dyn std::error::Error>> {
        let data = std::env::var_os("TODAY_WECHAT_DATA_DIR")
            .map(std::path::PathBuf::from)
            .unwrap_or(app.path().app_local_data_dir()?);
        let installed = std::env::current_exe()?
            .parent()
            .ok_or("missing executable directory")?
            .join("reader");
        let binary = if installed.is_file() {
            installed
        } else {
            // Fixed development fallback; never supplied by the webview.
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("binaries/reader-aarch64-apple-darwin")
        };
        let mut child = Command::new(binary)
            .process_group(0)
            .args(["--data-dir"])
            .arg(data)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        let input = child.stdin.take();
        let output = child.stdout.take().ok_or("missing output")?;
        let worker = Arc::new(Self {
            child: Mutex::new(child),
            input: Mutex::new(input),
            pending: Mutex::new(HashMap::new()),
            streams: Mutex::new(HashMap::new()),
            counter: AtomicU64::new(0),
            closing: AtomicBool::new(false),
            dirty: AtomicBool::new(false),
        });
        let weak = Arc::downgrade(&worker);
        std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else { break };
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    break;
                };
                if value.get("version").and_then(Value::as_u64) != Some(1) {
                    break;
                }
                if let Some(owner) = weak.upgrade() {
                    if let Some(id) = value.get("stream_id").and_then(Value::as_str) {
                        let terminal = matches!(value["event"]["type"].as_str(), Some("RUN_FINISHED" | "RUN_ERROR"));
                        let mut streams = owner.streams.lock().unwrap();
                        if let Some(channel) = streams.get(id) { let _ = channel.send(value.clone()); }
                        if terminal { streams.remove(id); }
                        continue;
                    }
                    if let Some(id) = value.get("id").and_then(Value::as_str) {
                        if let Some(sender) = owner.pending.lock().unwrap().remove(id) {
                            let reply = if let Some(e) = value.get("error") {
                                Err(BridgeError {
                                    code: e["code"].as_str().unwrap_or("reader_failed").into(),
                                    message: e["message"]
                                        .as_str()
                                        .unwrap_or("读取程序返回异常，请重开应用。")
                                        .into(),
                                    retryable: e["retryable"].as_bool().unwrap_or(true),
                                })
                            } else if let Some(result) = value.get("result") {
                                Ok(result.clone())
                            } else {
                                Err(error(
                                    "invalid_response",
                                    "读取程序返回不完整，请重开应用。",
                                ))
                            };
                            let _ = sender.send(reply);
                        }
                    }
                } else {
                    break;
                }
            }
            if let Some(owner) = weak.upgrade() {
                for (_, channel) in owner.streams.lock().unwrap().drain() {
                    let _ = channel.send(json!({"transport_error":true,"event":{"type":"RUN_ERROR","code":"reader_stopped","message":"本地程序连接已断开；已保存片段保留，请重开查看，不会自动重发。"}}));
                }
                for (_, sender) in owner.pending.lock().unwrap().drain() {
                    let _ = sender.send(Err(error(
                        "reader_stopped",
                        "本地读取程序已停止；数据保留，请重开应用。",
                    )));
                }
            }
        });
        Ok(worker)
    }

    async fn request(&self, command: String, params: Value, channel: Option<Channel<Value>>) -> Reply {
        const ALLOWED: &[&str] = &[
            "ask.model.save", "ask.model.enable", "ask.model.delete", "ask.model.test", "ask.model.calls",
            "ask.list", "ask.detail", "ask.preview", "ask.start", "ask.stop", "ask.revoke",
            "state",
            "source.local",
            "source.encrypted",
            "source.refresh",
            "chat.save",
            "chat.bulk.preview",
            "chat.bulk.apply",
            "model.calls",
            "tag.save",
            "tag.delete",
            "range.save",
            "sync.now",
            "sync.pause",
            "sync.resume",
            "messages.list",
            "messages.chats",
            "message.detail",
            "model.save",
            "model.enable",
            "model.delete",
            "analysis.real.preview",
            "analysis.real",
            "analysis.review.preview",
            "analysis.review",
            "task.change.resolve",
            "authorization.preview",
            "authorization.save",
            "authorization.revoke",
            "analysis.batch.retry",
            "tasks.list",
            "today.list",
            "task.update",
            "task.claim_self",
            "meeting.save",
            "meeting.list",
            "meeting.detail",
            "meeting.preview",
            "meeting.generate",
            "meeting.publish",
            "daily.preview",
            "daily.enable",
            "daily.disable",
            "daily.preview_run",
            "daily.run",
            "daily.detail",
            "daily.list",
        ];
        if !ALLOWED.contains(&command.as_str()) || !params.is_object() {
            return Err(error("invalid_command", "该操作不受支持。"));
        }
        if self.closing.load(Ordering::SeqCst) {
            return Err(error("reader_stopped", "应用正在退出，未开始新的操作。"));
        }
        let id = format!("desktop-{}", self.counter.fetch_add(1, Ordering::SeqCst));
        let line = format!(
            "{}\n",
            json!({"version":1,"id":id,"command":command,"params":params})
        );
        if line.len() > 2 * 1024 * 1024 {
            return Err(error("request_large", "请求过大，未执行操作。"));
        }
        let (tx, rx) = oneshot::channel();
        self.pending.lock().unwrap().insert(id.clone(), tx);
        if let Some(channel) = channel { self.streams.lock().unwrap().insert(id.clone(), channel); }
        let written = self
            .input
            .lock()
            .unwrap()
            .as_mut()
            .map(|input| input.write_all(line.as_bytes()));
        if !matches!(written, Some(Ok(()))) {
            self.pending.lock().unwrap().remove(&id);
            self.streams.lock().unwrap().remove(&id);
            return Err(error(
                "reader_stopped",
                "无法联系本地读取程序；请重开应用。",
            ));
        }
        match tokio::time::timeout(Duration::from_secs(300), rx).await {
            Ok(Ok(reply)) => {
                if reply.is_err() { self.streams.lock().unwrap().remove(&id); }
                reply
            },
            _ => {
                self.pending.lock().unwrap().remove(&id);
            self.streams.lock().unwrap().remove(&id);
                Err(error(
                    "response_unknown",
                    "暂未取得操作结果；请刷新状态核对，不要重复提交。",
                ))
            }
        }
    }

    fn shutdown(&self) {
        if self.closing.swap(true, Ordering::SeqCst) {
            return;
        }
        self.input.lock().unwrap().take(); // EOF requests safe cancellation.
        let deadline = Instant::now() + Duration::from_secs(5);
        let mut child = self.child.lock().unwrap();
        while Instant::now() < deadline {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        // Bounded fallback. SQLite rolls back any uncommitted transaction.
        // Only the new process group created above: covers PyInstaller's
        // bootloader and reader without touching unrelated user processes.
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
        let _ = child.wait();
    }
}

#[tauri::command]
async fn local_request(
    worker: tauri::State<'_, Arc<Worker>>,
    command: String,
    params: Value,
) -> Reply {
    worker.request(command, params, None).await
}

#[tauri::command]
async fn ask_stream(worker: tauri::State<'_, Arc<Worker>>, params: Value, on_event: Channel<Value>) -> Reply {
    worker.request("ask.start".into(), params, Some(on_event)).await
}

#[tauri::command]
fn set_dirty(worker: tauri::State<'_, Arc<Worker>>, dirty: bool) {
    worker.dirty.store(dirty, Ordering::SeqCst);
}

#[tauri::command]
async fn pick_source(app: tauri::AppHandle, encrypted: Option<bool>) -> Result<Option<String>, BridgeError> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog()
            .file()
            .set_title(if encrypted.unwrap_or(false) { "选择加密数据库目录（不会立即读取）" } else { "选择已解密数据库目录（不会立即读取）" })
            .blocking_pick_folder()
            .map(|path| {
                path.into_path()
                    .map(|path| path.to_string_lossy().into_owned())
                    .map_err(|_| error("invalid_source", "无法取得所选目录路径。"))
            })
            .transpose()
    })
    .await
    .map_err(|_| error("dialog_failed", "目录选择未完成，请重试。"))?
}

#[tauri::command]
async fn pick_keys_file(app: tauri::AppHandle) -> Result<Option<String>, BridgeError> {
    tauri::async_runtime::spawn_blocking(move || {
        app.dialog().file()
            .set_title("选择已有 JSON 密钥文件（不会立即读取）")
            .add_filter("JSON 密钥文件", &["json"])
            .blocking_pick_file()
            .map(|path| path.into_path()
                .map(|path| path.to_string_lossy().into_owned())
                .map_err(|_| error("invalid_source", "无法取得所选文件路径。")))
            .transpose()
    }).await.map_err(|_| error("dialog_failed", "密钥文件选择未完成，请重试。"))?
}

#[tauri::command]
fn finish_quit(app: tauri::AppHandle, worker: tauri::State<'_, Arc<Worker>>) {
    worker.dirty.store(false, Ordering::SeqCst);
    app.exit(0);
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let worker = Worker::launch(app.handle())?;
            app.manage(worker);
            // macOS's predefined Quit sends terminate: directly. Use a
            // normal menu event so the webview can protect unsaved edits.
            let menu = tauri::menu::Menu::default(app.handle())?;
            #[cfg(target_os = "macos")]
            if let Some(app_menu) = menu
                .items()?
                .first()
                .and_then(|item| item.as_submenu())
                .cloned()
            {
                let position = app_menu
                    .items()?
                    .len()
                    .checked_sub(1)
                    .ok_or("missing app menu")?;
                app_menu.remove_at(position)?;
                let quit = tauri::menu::MenuItem::with_id(
                    app,
                    "request-quit",
                    "退出 wechat-today",
                    true,
                    Some("CmdOrCtrl+Q"),
                )?;
                app_menu.append(&quit)?;
            }
            app.set_menu(menu)?;
            Ok(())
        })
        .on_menu_event(|app, event| {
            if event.id().as_ref() == "request-quit" {
                let _ = app.emit("request-quit", ());
            }
        })
        .invoke_handler(tauri::generate_handler![
            local_request,
            ask_stream,
            set_dirty,
            finish_quit,
            pick_source,
            pick_keys_file
        ])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                // Ask the current UI state; do not race an asynchronous
                // dirty flag update immediately after typing.
                let _ = window.emit("request-quit", ());
            }
        })
        .build(tauri::generate_context!())
        .expect("Unable to start wechat-today");
    app.run(|app, event| match event {
        tauri::RunEvent::ExitRequested { api, .. } => {
            if app.state::<Arc<Worker>>().dirty.load(Ordering::SeqCst) {
                api.prevent_exit();
                let _ = app.emit("request-quit", ());
            }
        }
        tauri::RunEvent::Exit => app.state::<Arc<Worker>>().shutdown(),
        _ => {}
    });
}
