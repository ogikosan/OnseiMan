"""Fun-ASR Nano Multilingual (sherpa-onnx ONNX int8) モデル自動ダウンロードスクリプト。"""
from __future__ import annotations

import argparse
import sys
import tarfile
import urllib.request
from pathlib import Path

MODEL_NAME = "sherpa-onnx-funasr-nano-int8-2025-12-30"
MODEL_ARCHIVE = f"{MODEL_NAME}.tar.bz2"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    f"{MODEL_ARCHIVE}"
)


def _progress_hook(block_num: int, block_size: int, total_size: int) -> None:
    if total_size <= 0:
        return
    downloaded = min(block_num * block_size, total_size)
    pct = downloaded * 100.0 / total_size
    mb_done = downloaded / (1024 * 1024)
    mb_total = total_size / (1024 * 1024)
    sys.stdout.write(f"\rダウンロード中: {pct:5.1f}% ({mb_done:6.1f} / {mb_total:6.1f} MB)")
    sys.stdout.flush()


def download_model(dest_root: Path, keep_archive: bool = False) -> Path:
    dest_root.mkdir(parents=True, exist_ok=True)
    target_dir = dest_root / MODEL_NAME
    if target_dir.exists() and any(target_dir.iterdir()):
        print(f"モデルは既に存在します: {target_dir}")
        return target_dir

    archive_path = dest_root / MODEL_ARCHIVE
    if not archive_path.exists():
        print(f"ダウンロード元: {MODEL_URL}")
        print(f"保存先: {archive_path}")
        try:
            urllib.request.urlretrieve(MODEL_URL, archive_path, reporthook=_progress_hook)
            print()
        except Exception as exc:
            if archive_path.exists():
                archive_path.unlink()
            raise RuntimeError(f"モデルのダウンロードに失敗しました: {exc}") from exc
    else:
        print(f"既存のアーカイブを使用します: {archive_path}")

    print("展開中...（少し時間がかかります）")
    with tarfile.open(archive_path, "r:bz2") as tar:
        # フォルダの外への書き込み・リンク・特殊なファイルを拒む安全な展開（Python 3.11.4 以降）
        tar.extractall(path=dest_root, filter="data")

    if not keep_archive and archive_path.exists():
        archive_path.unlink()

    print(f"完了しました: {target_dir}")
    return target_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Fun-ASR Nano ONNX int8 モデルをダウンロードします")
    parser.add_argument(
        "--dest",
        type=Path,
        default=Path(__file__).resolve().parent / "models",
        help="モデルの保存先ディレクトリ (デフォルト: ./models)",
    )
    parser.add_argument(
        "--keep-archive",
        action="store_true",
        help="展開後に .tar.bz2 ファイルを残す",
    )
    args = parser.parse_args()
    download_model(args.dest, keep_archive=args.keep_archive)


if __name__ == "__main__":
    main()
