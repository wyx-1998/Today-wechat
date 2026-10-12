# 测试说明

自动回归使用合成资料、临时 SQLite 和受控模型响应，不读取私人微信数据，不向真实模型服务发送聊天。检查协议和流程通过，仍需要另外评估真实语义质量。

## 后端回归

在项目根目录准备 Python 环境：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-refresh-test.txt
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -q
.venv/bin/python -W error::ResourceWarning -m unittest tests.model_smoke tests.reanalysis_smoke tests.automatic_smoke -q
```

标准回归覆盖来源身份、同步范围、消息查询、待办候选、事务、Ask、会议纪要与资讯日报。独立模型、重新整理和自动整理模块另查发送授权、迁移、状态保护、实际请求计数和失败恢复。

读取样例位于 `fixtures/reader_inputs.json` 和 `fixtures/reader_expected.json`。它们使用合成账号、独立期望与故障注入，不能把当前实现的输出自动当作正确答案。

## 前端

```bash
cd apps/desktop
npm ci
npm test
npm run build
```

前端检查包含范围、界面状态、原文查看、停止、事件契约和发送确认。`visual/` 中的页面用于隔离浏览器检查，不替代原生 WebView 操作验证。

## macOS 合成加密检查

以下模块使用公开合成密钥和自行生成的 AES/SQLite 数据，需安装 `requirements-refresh-test.txt`，平台限制明确失败，不把跳过算成通过：

```bash
.venv/bin/python -W error::ResourceWarning -m unittest tests.encrypted_refresh_smoke tests.encrypted_service_smoke -q
```

它们验证本项目处理的固定格式与恢复流程，不证明所有微信或 SQLCipher 版本兼容。

## 桌面包检查

完成桌面构建后，可在项目根目录执行：

```bash
.venv/bin/python -W error::ResourceWarning -m tests.packaged_reader_smoke --binary apps/desktop/src-tauri/target/debug/bundle/macos/wechat-today.app/Contents/MacOS/reader
.venv/bin/python -W error::ResourceWarning -m tests.packaged_ask_smoke --binary apps/desktop/src-tauri/target/debug/bundle/macos/wechat-today.app/Contents/MacOS/reader
```

包内检查使用隔离目录，确认打包的读取程序可以独立执行。Ask 包内检查覆盖无证据、授权和事件流程，不访问真实模型服务。

## 真实来源工具

`real_sync_*.py`、`product_data_check.py` 和部分审计命令属于显式手工检查工具。它们需要提供真实路径或指定范围，不加入默认自动回归。运行前确认参数和备份规则；输出与截图保留在本地，不提交到仓库。

提交问题时提供合成复现、错误代码和版本。不要附带数据库、真实消息、账号、会话标识、密钥、请求正文或私人文件路径。
