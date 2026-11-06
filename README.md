<p align="center">
  <img src="logo.png" width="120" alt="Breeze Blue TTS">
</p>

<h1 align="center">astrbot_plugin_breeze_tts</h1>

接入 [Breeze Blue](https://breezeblue.ai) 语音合成的 AstrBot 插件，可直接作为
[private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)
（「我会永远陪着你」）的语音后端，也支持独立使用。

## 功能

- **LLM 工具 `breeze_tts_speak`**：模型可主动把一句话合成语音发出
- **命令 `/breeze 文字`**：手动测试合成
- **作为 PC 的 TTS 后端**：实现 `synthesize_text()` 接口，被 private_companion 的
  `mimo_voice_clone` 后端自动发现
- 支持克隆音色、`instructions` 自然语言风格控制、音色名称映射

## 安装

把本目录放到 AstrBot 的插件目录：

```bash
cd /path/to/AstrBot/data/plugins
git clone https://github.com/<your-name>/astrbot_plugin_breeze_tts.git
```

重启 AstrBot，或在 WebUI 插件页重载。

## 配置

在 WebUI 插件配置页填写，或编辑 `data/config/astrbot_plugin_breeze_tts_config.json`：

```json
{
  "api_key": "brz_xxxxxxxxxxxxxxxx",
  "default_voice_id": "voc_xxxxxxxxxxxx",
  "voice_map": {
    "旁白": "voc_xxxxxxxxxxxx"
  },
  "model_id": "breeze-tts-2",
  "instructions": "",
  "guidance_scale": 4.0,
  "timeout": 120,
  "max_chars": 300,
  "voice_cache_ttl": 600
}
```

| 字段 | 说明 |
|---|---|
| `api_key` | 在 Breeze 控制台的 API keys 页面创建，形如 `brz_...` |
| `default_voice_id` | 默认音色 ID，形如 `voc_...` |
| `voice_map` | 「名称 → 音色 ID」映射，配置后可按名称引用，且无需联网查询 |
| `model_id` | `breeze-tts-2`（中/英）或 `breeze-tts-2-multilingual`（多语种） |
| `instructions` | 默认风格指令，自然语言描述语气 |
| `guidance_scale` | 风格强度，越高越贴近 instructions，建议 3–5 |
| `timeout` | 合成超时秒数 |
| `max_chars` | 单次合成字符上限，超出会截断 |

### 获取音色 ID

```bash
curl -4 -s https://api.breeze.blue/v1/voices \
  -H "xi-api-key: $BREEZE_API_KEY" \
  | python3 -c "import json,sys; [print(v['voice_id'], v['name']) for v in json.load(sys.stdin)['voices']]"
```

> 注意加 `-4`：`api.breeze.blue` 的 IPv6 路由较差，双栈时会显著变慢。

## 接入 private_companion

在 PC 的配置里设置：

```
enable_tts_enhancement = true
tts_synthesis_backend  = "mimo_voice_clone"
tts_mimo_tool_name     = "breeze_tts_speak"
tts_mimo_voice_name    = "旁白"        # 需在 voice_map 里存在，或用 voc_ 开头的 ID
```

PC 的发现机制：按 `tts_mimo_tool_name` 找到本插件的工具 → 沿 `handler.__self__`
取得插件实例 → 调用 `synthesize_text()` → 取回本地音频路径。
`readiness()` 会校验 `tts_mimo_voice_name` 是否存在于 `list_available_voices()` 中，
所以填写的名称必须能在 `voice_map` 或线上音色列表里找到。

`tts_mimo_style_prompt` 会作为 Breeze 的 `instructions` 传入，可用于控制语气。

## 实现说明

- **强制 IPv4**：`TCPConnector(family=AF_INET)`。不加的话首个字节可能延迟十几秒。
- **音色列表缓存**：`GET /v1/voices` 较慢，`list_available_voices()` 必须同步返回，
  因此只在启动后惰性拉取并缓存；缓存为空时用 `voice_map` 兜底。
- **音频落盘**：默认写到 `data/plugin_data/astrbot_plugin_breeze_tts/audio/`，
  自动清理 2 小时前的文件。

## 已知限制

- 合成耗时随文本长度增长，实测短句 6–20 秒；账号并发受限时可能更慢
- 音频完整返回后才发送，不做流式播放
- 仅实现「整段文本 → 音频文件」，不支持实时对话流

## License

MIT
