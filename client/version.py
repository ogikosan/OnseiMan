"""OnseiMan のバージョン。新しく公開するときは、ここを上げて変更点.md に節を書いてから python packaging\\release.py を実行する。"""
__version__ = "1.2.27"

# 最新版の情報（latest.json）を置いている場所。配布サイト（Cloudflare Worker）の URL。
# 空なら更新の確認をしない。
UPDATE_URL = ""  # 配布サイトは 2026-11-22 に削除（友達にはソース一式を直接渡す）
