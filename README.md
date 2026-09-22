# wechat-memory-agent

个人微信"文件助手"知识库 agent：把你发给微信 bot 的内容（文字/文件/链接/图片）静默归档，
之后可以用自然语言检索找回。

设计文档见 [docs/DESIGN.md](docs/DESIGN.md)。

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows
# .venv/bin/pip install -r requirements.txt     # macOS/Linux

# 2. 配置（复制并填写）
cp .env.example .env
# 填入 WMA_LLM_BASE_URL / WMA_LLM_API_KEY / WMA_LLM_MODEL

# 3. 启动（单一入口）
.venv/Scripts/python scripts/run.py

# 启动逻辑：
#   - 检测到 .env 有 WEIXIN_ACCOUNT_ID / WEIXIN_TOKEN → 直接连接启动
#   - 没有 → 自动进入扫码登录（终端显示二维码，用微信扫码），
#            登录成功自动保存密钥到 .env，然后继续启动
```

## 使用方式

- **普通消息**（文字/文件/链接/图片）→ 静默归档入库
- **`/` + 自然语言** → 检索，如：
  - `/帮我查一下最近一个月的压缩包`
  - `/那个腾讯的开源项目`
  - `/RAG`
- **`/1` `/2` ...** → 选择上一条检索结果，回传原文件
- **`/help`** → 帮助

## 数据存储

默认存在 `~/wechat-memory-agent-data/`（可用 `WMA_DATA_HOME` 覆盖）：

```
raw/            # 原文（永不改动）：每条消息一个目录
extracted/      # 提取文本（可重建）
index.db        # SQLite 索引（元数据 + FTS5 全文）
```

## 架构

```
微信 iLink 长轮询
   → 消息解析（文字/文件/链接/图片）
   → 类型分流（后缀+魔数）
   → 入库管线（OCR/文本提取/链接快照/LLM 元数据）
   → SQLite 索引（LLM 元数据 FTS5 + 全文 FTS5）
   → /自然语言 检索 → 卡片列表 → /序号 回传原文
```

详见 [docs/DESIGN.md](docs/DESIGN.md)。
