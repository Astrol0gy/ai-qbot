#!/usr/bin/env bash
# 下载表情包 CLIP 检索用的 Chinese-CLIP 量化 ONNX 模型。
#
#   bash scripts/download_sticker_model.sh [目标目录]
#
# 默认目录：<项目根>/data/models/chinese-clip-vit-base-patch16
# 运行时依赖：onnxruntime numpy Pillow tokenizers（见 requirements.txt）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$ROOT/data/models/chinese-clip-vit-base-patch16}"
HF="${STICKER_MODEL_BASE:-https://hf-mirror.com/Xenova/chinese-clip-vit-base-patch16/resolve/main}"
MS="https://www.modelscope.cn/models/AI-ModelScope/chinese-clip-vit-base-patch16/resolve/master"

mkdir -p "$DEST"

fetch() {
    local url="$1" out="$2"
    for _ in 1 2 3; do
        if wget -q --show-progress -c --timeout=90 --tries=2 -O "$out" "$url"; then
            return 0
        fi
        sleep 2
    done
    return 1
}

echo "模型目录：$DEST"
fetch "$HF/onnx/model_quantized.onnx" "$DEST/model_quantized.onnx"
fetch "$HF/tokenizer.json" "$DEST/tokenizer.json"
fetch "$HF/tokenizer_config.json" "$DEST/tokenizer_config.json"
# 两个小配置优先用 ModelScope（国内更稳）；失败再回 hf-mirror。
fetch "$MS/preprocessor_config.json" "$DEST/preprocessor_config.json" \
    || fetch "$HF/preprocessor_config.json" "$DEST/preprocessor_config.json"
fetch "$MS/config.json" "$DEST/config.json" \
    || fetch "$HF/config.json" "$DEST/config.json"

ls -lh "$DEST"
echo "完成。可在项目根运行：python -m src.sticker_rag --build"
