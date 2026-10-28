# AI 私聊 Agent（微信 + 抖音）

一个**读屏驱动**的个人私聊回复助手：微信走窗口截图 + OCR，抖音走 CDP + DOM，
两边共用一个决策核心（去重、风险分级、出稿、人工审核、发送核验）。

> 面向**你自己已登录、已授权**的账号。不做 DLL 注入、不解密本地数据库、不调用非官方接口。
> 自动化操作私信有被判违规的风险，请自行评估并控制频率。

## 架构

```
微信窗口/OCR ──> adapters/wechat ──┐
                                   ├─> core（决策核心，SQLite）
抖音 CDP/DOM ──> adapters/douyin ──┘
```

核心只做决策，**不发送**；发送永远由适配器执行，并且必须通过 `presend` 硬检查。

```
observe → 去重 → 硬拦截 → 判断风险 → 路由 → （媒体）读"她想要什么" → 出稿
        → 人工审核 / 自动待发 → 适配器发送 → 核验 → 标记 DONE / SEND_UNKNOWN
```

## 核心能力

| 能力 | 说明 |
|---|---|
| 统一消息模型 | 平台无关，带证据等级（界面观测 / 界面核验 / 平台 ID） |
| 去重 | 原生 ID 优先，否则内容指纹 + 媒体签名；SQLite 唯一约束兜底 |
| 状态机 | 12 个正常态 + 6 个异常态，非法迁移直接报错 |
| 风险分级 | R0–R3，**规则先行**，模型只能加重不能减轻 |
| 人工审核 | 队列、批准/驳回、`reconcile` 修异常态 |
| 限流/熔断 | 同会话最小间隔、每日上限、连续失败熔断 |
| 引用校验 | 发送前 OCR 引用条，必须同时匹配联系人与内容 |
| 发送核验 | 输入框清空 + 气泡出现；状态不明记 `SEND_UNKNOWN`，**禁止直接重发** |
| 记忆 | 近期上下文 + 长期事实，**按联系人隔离**；显式配置才跨平台合并 |
| 视频理解 | 抖音：语音转写 + 画面字幕；微信：引用条文案 + 缩略图 OCR |
| 关系画像 | 从真实聊天统计 + 抽样，产出按联系人隔离的"怎么回"笔记 |

## 目录

```
core/                决策核心（Python 标准库 + SQLite）
  model/dedupe/store  数据与持久化
  risk/route/verify   风险、路由、发送校验
  desire/profile      她想要什么、关系画像
  prompt/persona/memory  提示词、人设、记忆
  cli.py              唯一入口
tests/               140 个单测
config/              示例配置（settings.json / persona.json）
adapters/wechat/     微信适配器（截图、OCR、发送、批量扫描）
adapters/douyin/     抖音适配器（CDP、媒体下载、引用发送）
```

## 快速开始

```bash
python -m unittest discover -s tests -t .
python -m core.cli selftest          # 离线端到端自检，不联网不发消息
python -m core.cli config-check
```

配置：

- `config/settings.json`：`dry_run` 默认 `true`、`mode` 默认 `manual`、白名单为空
- `config/persona.json`：通用人设；私有的关系背景放 `persona.local.json`（不入库）
- `config/settings.local.json`：真实联系人、跨平台记忆合并（不入库）

## 安全模型

1. **默认不发送**：`dry_run=true` + `mode=manual`，所有草稿进审核队列。
2. **白名单**：只有 `reply.allowlisted_contacts` 里的人会进入流程；备注带后缀也能识别。
3. **R2 必须人工**：金钱、隐私、分手/关系决定、重大承诺、激烈冲突、危机表达。
4. **R3 直接停**：联系人不确定、界面状态不明、引用未验证。
5. **引用硬校验**：引用条必须同时包含联系人名和内容片段，否则不按发送。
6. **发送结果不明不重发**：记 `SEND_UNKNOWN`，人工复核后再决定。
7. **限流**：同会话 5 分钟、每天 20 条、连续失败 3 次熔断。

## 已知限制

- 读屏依赖窗口布局，微信改版可能失效；引用目标只能做到**界面级验证**。
- 抖音聊天播放器没有评论区，**热评拿不到**；文案也只有作者名。
- OCR 会认错字（人名、专有名词），所以风险等级默认偏保守。
- 图片/图集分支未充分实测。
- 适配器里的路径需要按你的环境修改。

## 免责声明

仅供个人学习与自用。请遵守平台用户协议与当地法律；由使用产生的账号风险与后果由使用者自行承担。

## 致谢

思路与部分工程实践参考了这些开源项目：

- [formulahendry/wechat-acp](https://github.com/formulahendry/wechat-acp) — 微信 iLink 桥接与会话管理
- [863401402/she-love-me](https://github.com/863401402/she-love-me) — 聊天统计 + 分层采样的分析框架
- [yzfly/douyin-mcp-server](https://github.com/yzfly/douyin-mcp-server) — 长音频分段与语音转写流程
- [jev-chat/jev-chat-windows](https://github.com/jev-chat/jev-chat-windows) — 气泡归属判定与"判断/起草"分离
- [wangrongding/wechat-bot](https://github.com/wangrongding/wechat-bot) — 白名单语义与多模型适配
- [freestylefly/WeChatBridge](https://github.com/freestylefly/WeChatBridge) — 原生转发导出思路
- [xlrpa/FlowBot](https://github.com/xlrpa/FlowBot) — 安卓无障碍方案的参考
