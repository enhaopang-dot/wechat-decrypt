# 微信本地数据解密与导出 · WeChat Local Data Toolkit

> 把**你自己电脑上**的微信聊天数据库解密、导出、分析。数据库是本地 SQLCipher 加密的，这个工具解开它，让你能读、能搜、能导出。

**关键词**：微信 · 聊天记录导出 · 数据库解密 · WeChat · WeChatDB · SQLCipher · wcdb · 微信4.x · 聊天记录分析 · 语音转文字 · 朋友圈导出 · MCP Server · 本地数据 · digital forensics · chat export

---

## ⚠️ 使用前提（先读这一段）

**这个工具只能用于处理你自己设备上、你有权访问的数据。**

- 仅限本人设备、本人账号
- 用于分析自己在自己账号里的聊天记录
- **禁止**用于获取、查看、分析他人未经授权的通信内容
- 在多数司法辖区，未经同意读取他人通信可能构成违法

使用本工具产生的任何后果由使用者自行承担。仓库内**不含任何解密密钥、账号数据和聊天内容**，你需要自己从自己的设备提取。

如果你不同意这些前提，请不要使用。

---

## 这是什么

微信 PC 端的聊天数据库是本地的 SQLCipher 加密库。这个工具做四件事：

1. **提取密钥**：从运行中的微信进程内存里找到解密密钥
2. **解密数据库**：批量解密 message / contact / media 等分库
3. **导出聊天**：按会话导出文本、图片、语音、文件、合并转发
4. **检索分析**：全文搜索、按时间/联系人过滤、统计

## 功能清单

- 支持微信 4.x 的新版数据库结构（wcdb）
- 自动发现 `xwechat_files` 目录与账号分库
- 图片解密（.dat → jpg/png/gif）
- 语音转文字（可接本地或云端转写）
- 视频、文件、位置、名片、引用消息解析
- 朋友圈数据导出
- MCP Server：把聊天记录作为数据源暴露给 AI Agent 检索

## 快速开始

### 环境

- Windows + 微信 PC 端（已登录）
- Python 3.10+

```bash
pip install -r requirements.txt
```

### 1. 配置

复制 `config.example.json` 为 `config.json`，填入你自己的微信数据目录：

```json
{
  "wechat_dir": "<你的 xwechat_files 路径>",
  "output_dir": "<导出目录>"
}
```

### 2. 找密钥

```bash
python find_all_keys.py
```

密钥写入本地 `all_keys.json`，**这个文件不要提交、不要外传**。

### 3. 解密

```bash
python decrypt_db.py
```

### 4. 导出

```bash
python export_all_chats.py
```

按会话导出到 `output_dir`。

### 5. 语音转写（可选）

```bash
python transcribe_chat.py
```

## 目录

```
├── find_all_keys.py         从进程内存提取密钥
├── batch_decrypt*.py        批量解密数据库
├── export_all_chats.py      按会话导出聊天记录
├── decode_image.py          图片 .dat 解密
├── voice_to_mp3.py          语音转音频
├── transcribe_chat.py       整段会话转写
├── wechat_monitor.py        实时监控（轮询新消息）
├── mcp_server.py            把聊天记录暴露给 AI Agent
├── docs/                    格式说明与踩坑记录
└── tests/                   单元测试
```

## 安全与隐私

- **密钥文件 `all_keys.json` 只在你本机生成**，不进仓库、不外传
- 导出的聊天数据默认落在你指定的本地目录
- 如果要把数据喂给云端模型，请自行评估隐私风险

## 已知限制

- 微信版本更新可能改变数据库结构，工具需要跟进
- 部分消息类型（如小程序卡片）解析不完整
- macOS 支持有限

## 授权

MIT License

## 联系

加微信：**enhaopang**

添加时请备注：GitHub-wechat-decrypt

> 不提供免费技术支持，也不协助任何未经授权的数据获取。
