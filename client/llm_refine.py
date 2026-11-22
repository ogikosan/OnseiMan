"""ローカル LLM（Ollama / OpenAI 互換 API）を使った AI 整形＆選択テキスト書き換え。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import requests

logger = logging.getLogger("onseiman.llm")

REFINE_SYSTEM_PROMPT = (
    "あなたは日本語の音声入力テキストを整えるエディターです。"
    "言い直し（例:「明日、あいや明後日」→「明後日」）、重複、口癖を自然に整理し、"
    "元の意味を保った読みやすい文章にしてください。"
    "前置き・解説・引用符・マークダウンの囲みは一切付けず、整形後の本文だけを出力してください。"
)

REWRITE_SYSTEM_PROMPT = (
    "あなたはテキスト編集アシスタントです。"
    "ユーザーが選択した元テキストを、音声の指示に従って書き換えてください。"
    "前置き・解説・引用符・余計なマークダウン囲みは一切付けず、書き換え後のテキストだけを出力してください。"
)

SELF_CORRECTION_SYSTEM_PROMPT = (
    "あなたは音声入力の言い直しを整理するエディターです。"
    "話者が途中で「いやいやそうじゃなくて」「じゃなくて」「違う」「訂正」「あ、〇〇だった」などで言い直した場合、"
    "取り消された古い内容と言い直しフレーズを消し、最新の意図だけを自然な日本語1文にまとめてください。\n"
    "入力: 明日の3時に、いやいやそうじゃなくて4時に集合で。\n出力: 明日の4時に集合で。\n"
    "入力: 明日の3時に会議しよう。あ、明後日だった。\n出力: 明後日の3時に会議しよう。\n"
    "入力: 5090で動かして、あ、じゃなくてミニPCの方で動かして。\n出力: ミニPCの方で動かして。\n"
    "ルール: 前置きや説明は絶対に書かず、整理後の本文のみを出力すること。"
)


@dataclass
class LLMResult:
    text: str
    model: str
    elapsed_ms: float


class LLMRefiner:
    """Ollama (/api/chat) および OpenAI 互換 (/v1/chat/completions) の両方に対応。"""

    def __init__(
        self,
        base_url: str,
        model: str = "",
        api_key: str = "",
        timeout_sec: float = 25.0,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        self.model = (model or "").strip()
        self.api_key = (api_key or "").strip()
        self.timeout_sec = timeout_sec
        self._resolved_model: str = ""

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _detect_backend_and_model(self) -> tuple[str, str]:
        """利用可能なエンドポイント種別 ('ollama' | 'openai') とモデル名を返す。"""
        if not self.base_url:
            raise RuntimeError("AI 整形の接続先 URL が設定されていません（設定画面で指定してください）")

        chosen_model = self.model or self._resolved_model

        # URL に /v1 が含まれている場合は OpenAI 互換を優先
        if self.base_url.endswith("/v1") or "/v1/" in self.base_url:
            if not chosen_model:
                chosen_model = self._fetch_openai_first_model()
            return "openai", chosen_model

        # Ollama (/api/tags) を確認
        try:
            r = requests.get(f"{self.base_url}/api/tags", timeout=2.0)
            if r.status_code == 200:
                models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
                if not chosen_model:
                    if not models:
                        raise RuntimeError("Ollama にモデルが入っていません（例: `ollama pull qwen2.5:7b`）")
                    chosen_model = models[0]
                    self._resolved_model = chosen_model
                return "ollama", chosen_model
        except requests.RequestException:
            pass

        # OpenAI 互換 (/v1/models) を確認
        if not chosen_model:
            chosen_model = self._fetch_openai_first_model()
        return "openai", chosen_model

    def _fetch_openai_first_model(self) -> str:
        url = f"{self.base_url}/models" if self.base_url.endswith("/v1") else f"{self.base_url}/v1/models"
        r = requests.get(url, headers=self._headers(), timeout=3.0)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            raise RuntimeError("LLM サーバーに利用可能なモデルが見つかりませんでした")
        model = str(data[0].get("id", "")).strip()
        if not model:
            raise RuntimeError("LLM サーバーからモデル名を取得できませんでした")
        self._resolved_model = model
        return model

    def _chat(self, system_prompt: str, user_content: str) -> LLMResult:
        t0 = time.perf_counter()
        kind, model = self._detect_backend_and_model()

        if kind == "ollama":
            url = f"{self.base_url}/api/chat"
            payload = {
                "model": model,
                "stream": False,
                "keep_alive": "30m",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "options": {"temperature": 0.1},
            }
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout_sec)
            resp.raise_for_status()
            content = str((resp.json().get("message") or {}).get("content") or "").strip()
        else:
            url = (
                f"{self.base_url}/chat/completions"
                if self.base_url.endswith("/v1")
                else f"{self.base_url}/v1/chat/completions"
            )
            payload = {
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            }
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=self.timeout_sec)
            resp.raise_for_status()
            choices = resp.json().get("choices") or []
            content = str(((choices[0].get("message") if choices else {}) or {}).get("content") or "").strip()

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return LLMResult(text=content, model=model, elapsed_ms=elapsed_ms)

    def refine_dictation(self, spoken_text: str) -> LLMResult:
        return self._chat(REFINE_SYSTEM_PROMPT, spoken_text)

    def rewrite_selection(self, selected_text: str, instruction: str) -> LLMResult:
        prompt = f"【元のテキスト】\n{selected_text}\n\n【指示】\n{instruction}"
        return self._chat(REWRITE_SYSTEM_PROMPT, prompt)

    def resolve_self_correction(self, spoken_text: str) -> LLMResult:
        return self._chat(SELF_CORRECTION_SYSTEM_PROMPT, spoken_text)
