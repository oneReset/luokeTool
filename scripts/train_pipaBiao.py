"""Train YOLO model on pipaBiao dataset."""
import os
import shutil

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ultralytics import YOLO

DATA_YAML = os.path.join(_BASE, "datasets", "pipaBiao", "data.yaml")
MODEL_PATH = os.path.join(_BASE, "yolo26s.pt")

if not os.path.exists(MODEL_PATH):
    MODEL_PATH = "yolo26s.pt"

print(f"数据: {DATA_YAML}")
print(f"预训练模型: {MODEL_PATH}")
print(f"开始训练 pipaBiao 模型...")

model = YOLO(MODEL_PATH)
import torch
_device = "0" if torch.cuda.is_available() else "cpu"
print(f"使用设备: {_device}")

model.train(
    data=DATA_YAML,
    epochs=100,
    batch=8,
    imgsz=640,
    device=_device,
    workers=0,
    plots=True,
    verbose=True,
    val=False,
    mosaic=1.0,
    flipud=0.5,
    fliplr=0.5,
    hsv_h=0.015,
    hsv_s=0.7,
    hsv_v=0.4,
    scale=0.5,
    translate=0.1,
    erasing=0.4,
)

# Copy trained model to models/
os.makedirs(os.path.join(_BASE, "models"), exist_ok=True)
src = os.path.join(_BASE, "runs", "detect", "train", "weights", "best.pt")
dst = os.path.join(_BASE, "models", "pipaBiao.pt")
if os.path.exists(src):
    shutil.copy2(src, dst)
    print(f"\n训练完成！模型已保存: {dst}")
else:
    print("\n训练完成！请手动将 best.pt 复制到 models/pipaBiao.pt")
