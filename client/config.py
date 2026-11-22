from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("onseiman.config")


@dataclass
class ClientConfig:
    hotkey: str = "ralt"
    # "toggle": 1回押して録音開始 → もう1回押して確定・入力（押しっぱなし不要）
    # "hold"  : 押している間だけ録音し、離したら確定・入力
    hotkey_mode: str = "toggle"
    # トグル録音時、発話を検出したあとに無音が続いたら自動で確定する。
    # 考えている間に確定してしまい不具合に見えるので、初期設定はオフ（もう一度キーを押すまで録音を続ける）
    auto_stop_on_silence: bool = False
    silence_stop_sec: float = 1.8
    silence_rms_threshold: float = 0.012
    # 「うん、」「えーと」などの言いよどみを取り除く（対象語は dict.json の fillers）
    remove_fillers: bool = True
    # 漢数字を数字に（五千九十→5090）、カタカナ略語を英字に（ピーディーエフ→PDF）などの標準の変換
    normalize_text: bool = True
    # 音声コマンド: 「改行」→改行、「かぎかっこ」「かっこ開き/閉じ」「はてなマーク」「びっくりマーク」→記号
    voice_commands: bool = True
    # 「今のなし」「取り消し」とだけ言うと、直前に入力した文を取り消す
    undo_voice_command: bool = True
    # 録音していない間もマイクを開いたままにし、キーを押す直前の声も録る（言い出しが欠けない・開始が速い）
    keep_mic_open: bool = True
    preroll_sec: float = 0.4
    # 話しながら、話の切れ目ごとに裏で認識しておく（長く話しても消えない・話し終えてからの待ちが短い）
    live_segmentation: bool = True
    segment_pause_sec: float = 0.6
    segment_min_sec: float = 8.0
    segment_max_sec: float = 18.0
    # 言い直し（「いやいやそうじゃなくて」「訂正」等）を検知して最新の意図だけ残す
    auto_refine_self_correction: bool = True
    # 文末の「。」をすべてのアプリで自動的に削る（「。。」「？」「！」は残す）
    strip_trailing_period: bool = True
    # チャット系アプリ（Discord / Slack / LINE 等）で文末の「。」を自動削除する
    app_specific_rules: bool = True
    # 貼り付け後にユーザーが直した箇所を感知して辞書学習候補にする（半自動）
    learn_from_edits: bool = True
    # 同じ修正をこの回数検知したら自動で辞書登録する（0 なら常に手動承認のみ）
    auto_learn_after_count: int = 2
    chat_apps: List[str] = field(
        default_factory=lambda: [
            "Discord.exe",
            "Slack.exe",
            "LINE.exe",
            "ms-teams.exe",
            "Teams.exe",
            "Telegram.exe",
            "Chatwork.exe",
        ]
    )
    # 第2ホットキー: AI 整形 / 選択テキスト書き換え（空文字なら無効）
    ai_hotkey: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_sec: float = 25.0
    cancel_key: str = "esc"
    suppress_hotkey: bool = True
    min_duration_sec: float = 0.3
    max_duration_sec: float = 120.0
    sample_rate: int = 16000
    microphone: Optional[int | str] = None
    # 認識する場所: "local"（このパソコン）/ "server"（別のパソコンのサーバー。詳しい人向け）
    # 空なら、サーバーの登録があれば "server"、なければ "local"（前の設定のまま動くように）
    recognition_place: str = ""
    server_urls: List[str] = field(default_factory=list)
    # サーバーのアドレス → パソコンの名前（「サーバーをさがす」で見つけたときに覚える。画面の表示用）
    server_names: Dict[str, str] = field(default_factory=dict)
    # いつも使うサーバー（server_urls のどれか）。止まっているときだけ、ほかのサーバーを使う。空なら最後に成功したもの
    preferred_server: str = ""
    server_connect_timeout_sec: float = 1.5
    # 辞書をほかのパソコンと合わせる: ""（合わせない）/ "server"（認識役のパソコンに預ける）/ "folder"（同期フォルダに置く）
    dict_share: str = ""
    # "folder" のときの置き場所（OneDrive などの中のフォルダ）
    dict_share_folder: str = ""
    # 預ける辞書の名前（英数字）。1 台のサーバーを何人かで使うとき、人ごとに分ける
    dict_share_name: str = "shared"
    server_read_timeout_sec: float = 30.0
    local_fallback: bool = True
    local_model_dir: str = "models/sherpa-onnx-funasr-nano-int8-2025-12-30"
    local_num_threads: int = 4
    language: str = ""
    itn: bool = True
    dict_path: str = "dict.json"
    restore_clipboard: bool = True
    paste_delay_ms: int = 50
    clipboard_restore_delay_ms: int = 180
    show_overlay: bool = True
    # 録音中などの表示を、ドラッグで決めた場所に出す [x, y]。null ならカーソルの左上
    overlay_position: Optional[List[int]] = None
    # 録音中の表示: "pill"（× ・ 音量 ・ ✓ の小さなバー）/ "text"（文字の案内）
    recording_indicator: str = "pill"
    # 録音している間、ほかのアプリ（動画・音楽など）の音を消す
    mute_other_audio: bool = True
    # 表示の基本の場所: "bottom"（画面下の中央）/ "caret"（入力中のカーソルの左上）
    overlay_place: str = "bottom"
    beep_enabled: bool = True

    @classmethod
    def load(cls, path: Path) -> "ClientConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("config.json の読み込みに失敗しました（デフォルトを使用）: %s", exc)
            return cls()

        valid_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
