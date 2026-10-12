# Today-wechat

聊天里的重要事情，有处可查，有序处理。

Today-wechat 是一个面向本地微信记录的桌面工作助手。你可以选择会话与时间范围，把已同步的文字整理成待办、关键要点和资讯日报，并随时回到原文核对。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## 功能

| 功能 | 使用方式 |
| --- | --- |
| Today | 集中查看需要行动的事项、关键要点和资讯日报 |
| 待办 | 查看来源、确认是否属于自己、维护状态；已有事项的变更先作为建议 |
| 本地同步 | 接入已解密目录，或使用自己已有密钥的加密目录；选择会话后同步 |
| 文字整理 | 手动预览并确认发送，或明确开启所选范围的持续自动整理 |
| 原文查询 | 按会话、日期和发送者查找已同步消息，并查看相邻上下文 |
| 会议纪要 | 粘贴文字或导入 UTF-8 TXT/Markdown，确认发送后生成，再选择加入要点和待办 |
| 资讯日报 | 独立确认范围后，按北京时间汇总前一天文字，支持历史与补齐 |
| Ask | 独立配置 OpenAI 兼容或 Gemini 原生服务，查询本地记录，支持流式回答、追问、停止与引用 |

这是开发中的桌面项目。现有检查以隔离数据和受控模型为主，不能保证待办无遗漏或模型判断始终正确。Ask 的真实服务兼容与语义质量仍需按实际配置验证。Pi Agent 接入目前是设计方向，尚未实现。

## 从源码构建

当前构建脚本已验证 macOS / Apple Silicon，其他系统和架构尚未验证。需要 Python 3、Node.js 22.18+、npm 和 Xcode Command Line Tools。首次构建需要联网下载 Rust、npm 和 Python 依赖。

```bash
git clone https://github.com/wyx-1998/Today-wechat.git
cd Today-wechat
bash scripts/build_desktop.sh
```

构建脚本在项目目录准备工具链和 Python 环境，再打包 React、Tauri 与 Python 读取程序。完成后启动：

```bash
bash scripts/launch_desktop.sh
```

应用位于 `apps/desktop/src-tauri/target/debug/bundle/macos/wechat-today.app`。当前为本机构建流程，未提供经过签名、公证的正式安装包；运行构建好的应用不需要另装 Python。

## 开始使用

1. 在“设置 → 本地同步”选择自己的来源目录。公开仓库不提供微信数据、密钥或进程密钥提取器。
2. 在会话设置中选择需要同步的会话，确认读取范围，再开始同步。
3. 如果需要模型整理，在“模型与用量”配置服务，预览实际发送内容并确认。Ask 使用独立问答配置与授权。
4. 打开 Today 或待办查看结果，通过原文依据核对负责人、期限和完成情况。
5. 导入会议纪要时，先保存原文，再确认发送；生成成功后选择需要加入的内容。

没有接入真实来源时，可使用应用内的合成演示来源。演示消息会标明合成，不代表真实同步结果。不要上传真实聊天记录来报告问题。

## 数据与隐私

源记录读取、本地查询和业务数据保存在本机。启用模型功能并确认发送后，所选文字会发送到你配置的模型服务；不能理解为聊天内容始终不出本机。脱敏可能漏检，发送前需要核对预览。

默认应用数据目录为 `~/Library/Application Support/cn.wechat-today.desktop/`。当前模型密钥在本机 SQLite 中以明文保存，保护数据库及备份，也不要把应用数据目录加入 Git。数据库和聊天内容不受项目开源许可授权。

同步范围、整理授权、Ask 授权和单份会议纪要授权分别管理。没有找到交付记录，不等于任务没有完成；有可点击引用，也不等于模型结论正确。

## 开发与检查

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-refresh-test.txt
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -q
.venv/bin/python -W error::ResourceWarning -m unittest tests.model_smoke tests.reanalysis_smoke tests.automatic_smoke -q
```

前端检查和构建：

```bash
cd apps/desktop
npm ci
npm test
npm run build
```

测试默认使用临时合成数据库及受控响应。需要真实来源的手工检查工具必须显式指定参数，不属于上述自动回归。详见[测试说明](tests/README.md)与[架构说明](docs/ARCHITECTURE.md)。

## 目录

```text
apps/desktop/   React 界面、Tauri 宿主与桌面打包配置
backend/        本地读取、SQLite、模型调用、整理、Ask 和会议纪要
scripts/        构建、启动、读取兼容工具与审计工具
tests/          合成样例、后端检查与隔离界面辅助工具
docs/          公开架构说明
```

## 许可证与致谢

项目采用 [MIT License](LICENSE)，允许使用、修改、商用和再分发，需保留版权与许可声明。第三方依赖继续适用各自许可证。

早期采集与读取兼容脚本来自或基于 [huangserva/wechat-assistant](https://github.com/huangserva/wechat-assistant)，保留其 MIT 许可与来源说明。详细文件范围及依赖许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
