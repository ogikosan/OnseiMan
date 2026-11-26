"""AI で文章を整える・選んだ文を書き換える。

頼む先は 4 つから選ぶ: GPT（OpenAI）・Claude（Anthropic）・Gemini（Google）・自分で動かす AI（Ollama /
OpenAI と同じ形の API）。どれも requests で直接呼ぶ（更新では部品を入れ直さないので、新しいライブラリを足さない）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger("onseiman.llm")

# 画面に出す名前・はじめに入れるモデル名・API キーを作るページ
PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {"label": "GPT（OpenAI）", "model": "gpt-5.4-mini",
               "key_url": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Claude（Anthropic）", "model": "claude-haiku-5-5",
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "gemini": {"label": "Gemini（Google）", "model": "gemini-3.5-flash-lite",
               "key_url": "https://aistudio.google.com/apikey"},
    "ollama": {"label": "自分で動かす AI（Ollama など）", "model": "", "key_url": ""},
}
CLOUD = ("openai", "anthropic", "gemini")

OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

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

# 「試す」ボタンで整えてもらう文
TEST_TEXT = "えーと、明日の会議は、あ、じゃなくて明後日の3時からでお願いします"


@dataclass
class LLMResult:
    text: str
    model: str
    elapsed_ms: float


def _http_error(resp: requests.Response) -> RuntimeError:
    """よくある失敗を、何をすればよいかわかる言葉にする。"""
    code = resp.status_code
    detail = ""
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        detail = str((err.get("message") if isinstance(err, dict) else err) or "")[:200]
    except Exception:
        detail = (resp.text or "")[:200]
    if code in (401, 403) or (code == 400 and "api key" in detail.lower()):
        msg = "API キーが正しくありません（作り直したキーを入れてください）"
    elif code == 404:
        msg = "モデル名が見つかりません（「モデルを読み込む」で選び直してください）"
    elif code == 429:
        msg = "使える量の上限です（しばらく待つか、その会社のページで支払いの設定を確かめてください）"
    elif code >= 500:
        msg = "AI の会社の側でエラーが起きています（しばらくしてからもう一度）"
    else:
        msg = f"AI に断られました（{code}）"
    return RuntimeError(f"{msg}{'：' + detail if detail else ''}")


class LLMRefiner:
    def __init__(
        self,
        provider: str = "ollama",
        model: str = "",
        api_key: str = "",
        base_url: str = "",
        timeout_sec: float = 25.0,
    ) -> None:
        if provider not in PROVIDERS:
            raise RuntimeError("AI が選ばれていません（設定の「AI で整える」で選んでください）")
        self.provider = provider
        self.model = (model or "").strip() or PROVIDERS[provider]["model"]
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or "").strip().rstrip("/")
        self.timeout_sec = timeout_sec
        if provider in CLOUD and not self.api_key:
            raise RuntimeError("API キーが入っていません（設定の「AI で整える」で入れてください）")
        if provider == "ollama" and not self.base_url:
            raise RuntimeError("AI のアドレスが入っていません（設定の「AI で整える」で入れてください）")

    # ---- 呼び出し ----
    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout_sec)
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json()

    def _get(self, url: str, headers: dict, params: dict | None = None) -> dict:
        resp = requests.get(url, headers=headers, params=params, timeout=min(self.timeout_sec, 10.0))
        if resp.status_code >= 400:
            raise _http_error(resp)
        return resp.json()

    def _openai_headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _anthropic_headers(self) -> dict:
        return {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}

    def _gemini_headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    def _own_v1(self) -> str:
        """自分で動かす AI の、OpenAI と同じ形の API の入口（.../v1）。"""
        return self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"

    def _own_is_ollama(self) -> bool:
        if self.base_url.endswith("/v1") or "/v1/" in self.base_url:
            return False
        try:
            return requests.get(f"{self.base_url}/api/tags", timeout=2.0).status_code == 200
        except requests.RequestException:
            return False

    def _chat(self, system_prompt: str, user_content: str) -> LLMResult:
        t0 = time.perf_counter()
        model = self.model
        if self.provider == "openai":
            data = self._post(f"{OPENAI_URL}/chat/completions", {
                "model": model,
                "messages": [{"role": "system", "content": system_prompt},
                             {"role": "user", "content": user_content}],
            }, self._openai_headers())
            choices = data.get("choices") or []
            text = str(((choices[0].get("message") if choices else {}) or {}).get("content") or "")
        elif self.provider == "anthropic":
            data = self._post(f"{ANTHROPIC_URL}/messages", {
                "model": model,
                "max_tokens": 2048,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            }, self._anthropic_headers())
            text = "".join(b.get("text", "") for b in data.get("content") or [] if b.get("type") == "text")
        elif self.provider == "gemini":
            data = self._post(f"{GEMINI_URL}/models/{model}:generateContent", {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            }, self._gemini_headers())
            cands = data.get("candidates") or []
            parts = ((cands[0].get("content") if cands else {}) or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought"))
        else:
            if not model:
                model = self._first_own_model()
            messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}]
            if self._own_is_ollama():
                data = self._post(f"{self.base_url}/api/chat", {
                    "model": model, "stream": False, "keep_alive": "30m",
                    "messages": messages, "options": {"temperature": 0.1},
                }, self._openai_headers())
                text = str((data.get("message") or {}).get("content") or "")
            else:
                data = self._post(f"{self._own_v1()}/chat/completions",
                                  {"model": model, "temperature": 0.2, "messages": messages},
                                  self._openai_headers())
                choices = data.get("choices") or []
                text = str(((choices[0].get("message") if choices else {}) or {}).get("content") or "")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return LLMResult(text=text.strip(), model=model, elapsed_ms=elapsed_ms)

    # ---- モデルの一覧（画面の「モデルを読み込む」） ----
    def list_models(self) -> list[str]:
        if self.provider == "openai":
            ids = [str(m.get("id", "")) for m in self._get(f"{OPENAI_URL}/models", self._openai_headers()).get("data", [])]
            return sorted(i for i in ids if i.startswith("gpt") and not any(
                w in i for w in ("audio", "realtime", "image", "transcribe", "tts", "search")))
        if self.provider == "anthropic":
            data = self._get(f"{ANTHROPIC_URL}/models", self._anthropic_headers(), {"limit": 100}).get("data", [])
            return [str(m.get("id", "")) for m in data if m.get("id")]
        if self.provider == "gemini":
            data = self._get(f"{GEMINI_URL}/models", self._gemini_headers(), {"pageSize": 200}).get("models", [])
            return [str(m.get("name", "")).removeprefix("models/") for m in data
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        if self._own_is_ollama():
            data = self._get(f"{self.base_url}/api/tags", self._openai_headers()).get("models", [])
            return [str(m.get("name", "")) for m in data if m.get("name")]
        data = self._get(f"{self._own_v1()}/models", self._openai_headers()).get("data", [])
        return [str(m.get("id", "")) for m in data if m.get("id")]

    def _first_own_model(self) -> str:
        models = self.list_models()
        if not models:
            raise RuntimeError("AI にモデルが入っていません（例: `ollama pull qwen2.5:7b`）")
        self.model = models[0]
        return self.model

    # ---- 頼むこと ----
    def refine_dictation(self, spoken_text: str) -> LLMResult:
        return self._chat(REFINE_SYSTEM_PROMPT, spoken_text)

    def rewrite_selection(self, selected_text: str, instruction: str) -> LLMResult:
        prompt = f"【元のテキスト】\n{selected_text}\n\n【指示】\n{instruction}"
        return self._chat(REWRITE_SYSTEM_PROMPT, prompt)

    def resolve_self_correction(self, spoken_text: str) -> LLMResult:
        return self._chat(SELF_CORRECTION_SYSTEM_PROMPT, spoken_text)
