"""Breeze Blue TTS 接入插件。

对接 Breeze Blue 语音合成（https://api.breeze.blue/v1），并暴露 private_companion
「MiMo Voice Clone」后端所要求的公开合成服务，使其可直接作为陪伴插件的语音后端。

PC 侧的发现机制（private_companion/tts_enhancement.py）：
  1. 按 tts_mimo_tool_name 找到本插件注册的 LLM 工具 → 沿 handler.__self__ 取插件实例
  2. 实例需提供 synthesize_text(text, voice_name=..., context=..., user_id=..., group_id=...)
     返回值为本地音频路径
  3. readiness 会调用 list_available_voices() 校验音色，故该函数必须同步且快速

注意事项：
  - api.breeze.blue 的双栈路由较差，必须强制 IPv4，否则首字节可能拖到十几秒
  - Breeze 为 ElevenLabs 风格的 API：认证头 xi-api-key，合成路径含 voice_id
"""

import os
import socket
import time

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
except Exception:  # 兼容旧版本
    get_astrbot_plugin_data_path = None

API_BASE = "https://api.breeze.blue/v1"
DEFAULT_MODEL = "breeze-tts-2"


@register(
    "astrbot_plugin_breeze_tts",
    "local",
    "接入 Breeze Blue 语音合成，可作为 private_companion 的 TTS 后端",
    "1.0.0",
)
class BreezeTtsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

        self.api_key = str(self.config.get("api_key", "") or "").strip()
        self.default_voice_id = str(self.config.get("default_voice_id", "") or "").strip()
        self.model_id = str(self.config.get("model_id", DEFAULT_MODEL) or DEFAULT_MODEL).strip()
        self.instructions = str(self.config.get("instructions", "") or "").strip()

        voice_map = self.config.get("voice_map")
        self.voice_map: dict[str, str] = {}
        if isinstance(voice_map, dict):
            for name, vid in voice_map.items():
                name = str(name).strip()
                vid = str(vid).strip()
                if name and vid:
                    self.voice_map[name] = vid

        try:
            self.guidance_scale = float(self.config.get("guidance_scale", 4.0) or 4.0)
        except Exception:
            self.guidance_scale = 4.0
        try:
            self.timeout = max(20, int(self.config.get("timeout", 120) or 120))
        except Exception:
            self.timeout = 120
        try:
            self.max_chars = max(20, int(self.config.get("max_chars", 300) or 300))
        except Exception:
            self.max_chars = 300
        try:
            self.voice_cache_ttl = max(60, int(self.config.get("voice_cache_ttl", 600) or 600))
        except Exception:
            self.voice_cache_ttl = 600

        self.output_dir = self._resolve_output_dir()
        try:
            os.makedirs(self.output_dir, exist_ok=True)
        except Exception as exc:
            logger.warning("[BreezeTTS] 创建音频目录失败: %s", exc)

        self._voices_cache: list[dict] = []
        self._voices_fetched_at = 0.0

        # PC 的 readiness() 会读 plugin_config.api_key 判断是否已配置
        try:
            from types import SimpleNamespace

            self.plugin_config = SimpleNamespace(api_key=self.api_key)
        except Exception:
            self.plugin_config = None

        if not self.api_key:
            logger.warning("[BreezeTTS] 未配置 api_key，语音合成不可用")

    # ---------------- 基础工具 ----------------

    def _resolve_output_dir(self) -> str:
        if get_astrbot_plugin_data_path is not None:
            try:
                base = get_astrbot_plugin_data_path()
                return os.path.join(base, "astrbot_plugin_breeze_tts", "audio")
            except Exception:
                pass
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")

    def _headers(self) -> dict:
        return {"xi-api-key": self.api_key, "Content-Type": "application/json"}

    def _session(self) -> aiohttp.ClientSession:
        # 强制 IPv4：该域名 IPv6 路由较差，双栈下会显著变慢
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        return aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    def _cleanup_old(self, keep_seconds: int = 7200) -> None:
        try:
            now = time.time()
            for name in os.listdir(self.output_dir):
                path = os.path.join(self.output_dir, name)
                if os.path.isfile(path) and now - os.path.getmtime(path) > keep_seconds:
                    os.remove(path)
        except Exception:
            pass

    # ---------------- 音色 ----------------

    async def _fetch_voices(self) -> list:
        if not self.api_key:
            return []
        try:
            async with self._session() as sess:
                async with sess.get(API_BASE + "/voices", headers=self._headers()) as resp:
                    if resp.status != 200:
                        logger.warning("[BreezeTTS] 拉取音色失败: status=%s", resp.status)
                        return []
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.warning("[BreezeTTS] 拉取音色异常: %s", exc)
            return []

        raw = data.get("voices") if isinstance(data, dict) else data
        voices = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            vid = str(item.get("voice_id") or item.get("id") or "").strip()
            if not vid:
                continue
            voices.append(
                {
                    "id": vid,
                    "name": str(item.get("name") or vid).strip(),
                    "origin": str(item.get("origin") or ""),
                    "language": str(item.get("language") or ""),
                }
            )
        logger.info("[BreezeTTS] 音色列表已刷新: %d 个", len(voices))
        return voices

    def list_available_voices(self) -> list:
        """供 PC readiness 使用：必须同步且快速，因此只读缓存。

        缓存为空时用配置里的 voice_map 与默认音色兜底 —— 否则 PC 配置的音色名
        匹配不到条目，会判定 voice_not_found 而拒绝启用。
        """
        if self._voices_cache:
            return list(self._voices_cache)
        seeded = [
            {"id": vid, "name": name, "origin": "config", "language": ""}
            for name, vid in self.voice_map.items()
        ]
        known_ids = {v["id"] for v in seeded}
        if self.default_voice_id and self.default_voice_id not in known_ids:
            seeded.append(
                {
                    "id": self.default_voice_id,
                    "name": "默认音色",
                    "origin": "config",
                    "language": "",
                }
            )
        return seeded

    async def refresh_voices(self, force: bool = False) -> list:
        now = time.time()
        if (
            not force
            and self._voices_cache
            and now - self._voices_fetched_at < self.voice_cache_ttl
        ):
            return list(self._voices_cache)
        voices = await self._fetch_voices()
        if voices:
            self._voices_cache = voices
            self._voices_fetched_at = now
        return self.list_available_voices()

    async def _resolve_voice(self, name_or_id: str = "") -> str:
        wanted = (name_or_id or "").strip()
        if not wanted:
            return self.default_voice_id
        if wanted.startswith("voc_"):
            return wanted
        if wanted in self.voice_map:
            return self.voice_map[wanted]
        voices = await self.refresh_voices()
        for v in voices:
            if wanted in {v.get("name", ""), v.get("id", "")}:
                return v["id"]
        for v in voices:
            if wanted and wanted in str(v.get("name", "")):
                return v["id"]
        logger.warning("[BreezeTTS] 未匹配到音色 %s，回退默认", wanted)
        return self.default_voice_id

    # ---------------- 合成 ----------------

    async def _synthesize_bytes(self, text: str, voice_id: str, style: str = "") -> bytes:
        payload = {"text": text, "model_id": self.model_id}
        instructions = (style or "").strip() or self.instructions
        if instructions:
            payload["instructions"] = instructions
        payload["voice_settings"] = {"guidance_scale": self.guidance_scale}

        url = "%s/text-to-speech/%s" % (API_BASE, voice_id)
        async with self._session() as sess:
            async with sess.post(url, headers=self._headers(), json=payload) as resp:
                body = await resp.read()
                if resp.status != 200:
                    snippet = body[:300].decode("utf-8", "replace")
                    raise RuntimeError(
                        "Breeze TTS 失败 status=%s body=%s" % (resp.status, snippet)
                    )
                if not body:
                    raise RuntimeError("Breeze TTS 返回空音频")
                return body

    async def synthesize_text(
        self,
        text: str,
        voice_name: str | None = None,
        context: str = "",
        user_id: str = "",
        group_id: str = "",
        split: bool = False,
        **kwargs,
    ) -> str:
        """PC 侧调用入口：返回本地 mp3 路径。"""
        content = (text or "").strip()
        if not content:
            return ""
        if len(content) > self.max_chars:
            content = content[: self.max_chars].rstrip()
        if not self.api_key:
            raise RuntimeError("Breeze TTS 未配置 api_key")

        voice_id = await self._resolve_voice(voice_name or "")
        if not voice_id:
            raise RuntimeError(
                "Breeze TTS 未配置音色：请填写 default_voice_id，"
                "或用 voice_map 建立「名称 → 音色 ID」的映射"
            )

        audio = await self._synthesize_bytes(content, voice_id, context)
        self._cleanup_old()

        path = os.path.join(self.output_dir, "breeze_%d.mp3" % int(time.time() * 1000))
        with open(path, "wb") as f:
            f.write(audio)
        logger.info(
            "[BreezeTTS] 合成完成 voice=%s chars=%d bytes=%d",
            voice_id,
            len(content),
            len(audio),
        )
        return path

    # 兼容 PC 的备用入口名
    async def text_to_speech(self, text: str, **kwargs) -> str:
        return await self.synthesize_text(text, **kwargs)

    # ---------------- 对外入口 ----------------

    @llm_tool(name="breeze_tts_speak")
    async def breeze_tts_speak(
        self, event: AstrMessageEvent, text: str, voice: str = ""
    ) -> str:
        """用 Breeze Blue 把文字合成语音并发给用户。

        当需要以「声音」形式回复、或用户明确要求发语音时使用。

        Args:
            text(string): 要朗读的文字，建议 60 字以内
            voice(string): 可选音色名或音色 ID，留空用默认音色

        Returns:
            合成结果说明；成功时音频会一并发出。
        """
        content = (text or "").strip()
        if not content:
            return "没有可朗读的文字。"
        try:
            path = await self.synthesize_text(
                content,
                voice_name=voice or None,
                user_id=str(event.get_sender_id() or ""),
                group_id=str(getattr(event, "unified_msg_origin", "") or ""),
            )
        except Exception as exc:
            logger.warning("[BreezeTTS] 工具合成失败: %s", exc)
            return "语音合成失败：%s" % exc
        if not path:
            return "语音合成未返回音频。"

        try:
            from astrbot.api.message_components import Record

            await event.send(event.chain_result([Record(file=path)]))
            return "已发送语音。"
        except Exception as exc:
            logger.warning("[BreezeTTS] 发送语音失败: %s", exc)
            return "音频已生成但发送失败：%s" % exc

    @filter.command("breeze")
    async def breeze_command(self, event: AstrMessageEvent, text: str = ""):
        """测试 Breeze TTS：/breeze 你好"""
        content = (text or "").strip()
        if not content:
            yield event.plain_result("用法：/breeze 要朗读的文字")
            return
        try:
            path = await self.synthesize_text(
                content, user_id=str(event.get_sender_id() or "")
            )
        except Exception as exc:
            yield event.plain_result("合成失败：%s" % exc)
            return
        if not path:
            yield event.plain_result("未生成音频。")
            return
        from astrbot.api.message_components import Record

        yield event.chain_result([Record(file=path)])
