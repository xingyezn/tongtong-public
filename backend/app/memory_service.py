"""Conservative long-term-memory extraction from completed conversations."""

import json
import logging

import aiohttp

log = logging.getLogger("memory")


class MemoryService:
    def __init__(self, config, account_store):
        self.config = config
        self.store = account_store
        self._session = None

    @property
    def enabled(self):
        return bool(self.config.get("memory", {}).get("enabled", True))

    def _url(self):
        memory = self.config.get("memory", {})
        explicit = str(memory.get("compatible_url", "")).strip()
        if explicit:
            return explicit.rstrip("/") + "/chat/completions"
        workspace = self.config.get("dashscope", {}).get("workspace_id", "")
        return ("https://{}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/"
                "chat/completions").format(workspace)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def summarize_conversation(self, conversation_id):
        if not self.enabled or not conversation_id:
            return []
        data = self.store.conversation_for_memory(conversation_id)
        if not data or not data["messages"]:
            return []
        session = data["conversation"]
        user_id = session["user_id"]
        existing = self.store.list_memories(user_id, enabled_only=False)
        transcript = "\n".join(
            ("用户" if item["role"] == "user" else "AI") + "：" + item["content"]
            for item in data["messages"][-80:]
        )
        prompt = """你是长期记忆整理器。根据对话更新用户档案，只保存用户明确说出的、未来仍有帮助的稳定事实，例如姓名、称呼、长期偏好、生活习惯和明确目标。
不要推断或保存密码、密钥、验证码、精确地址、身份证件、财务账号、健康诊断、政治立场、宗教信仰等敏感信息。临时请求和本轮任务不要保存。
合并同一事实，若新内容明确修正旧内容则输出新值。只输出 JSON 对象，格式：{{"memories":[{{"category":"基本信息|偏好|习惯|目标|其他","label":"简短字段名","value":"简洁事实"}}]}}。没有值得保存的内容时输出 {{"memories":[]}}。

已有档案：
{}

本次对话：
{}""".format(json.dumps(existing, ensure_ascii=False), transcript)
        api_key = self.config.get("dashscope", {}).get("api_key", "")
        workspace = self.config.get("dashscope", {}).get("workspace_id", "")
        if not api_key or not workspace:
            log.warning("memory summary skipped: missing DashScope credentials")
            return []
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=90))
        payload = {
            "model": self.config.get("memory", {}).get("model", "qwen3.8-max"),
            "messages": [
                {"role": "system", "content": "你只做保守、可撤销的用户长期记忆整理。"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": "Bearer " + api_key,
            "X-DashScope-WorkSpace": workspace,
            "Content-Type": "application/json",
        }
        try:
            async with self._session.post(self._url(), headers=headers,
                                          json=payload) as response:
                body = await response.text()
                if response.status >= 300:
                    raise RuntimeError("memory model HTTP {}: {}".format(
                        response.status, body[:300]))
            result = json.loads(body)
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content) if isinstance(content, str) else content
            memories = parsed.get("memories", []) if isinstance(parsed, dict) else []
            saved = []
            limit = int(self.config.get("memory", {}).get("max_memories_per_run", 12))
            for item in memories[:max(1, min(30, limit))]:
                if not isinstance(item, dict):
                    continue
                saved.append(self.store.upsert_memory(
                    user_id, item.get("category"), item.get("label"),
                    item.get("value"), source_conversation_id=conversation_id))
            self.store.mark_memory_updated(conversation_id)
            log.info("memory summary complete: conversation=%s saved=%d",
                     conversation_id, len(saved))
            return saved
        except Exception:
            log.exception("memory summary failed: conversation=%s", conversation_id)
            return []
