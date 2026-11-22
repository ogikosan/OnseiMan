"""OnseiMan のバージョン。新しく配布するときはここを上げてから packaging\\exe-installer\\release.bat を実行する。"""
__version__ = "1.0.8"

# 最新版の情報（latest.json）を置いている場所。配布サイト（Cloudflare Worker）の URL。
# 空なら更新の確認をしない。
UPDATE_URL = ""  # 配布サイトは 2026-11-22 に削除（友達にはソース一式を直接渡す）
