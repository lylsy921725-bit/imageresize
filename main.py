"""Image batch resize & crop tool with Tkinter GUI and CLI."""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import importlib.util
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DEPENDENCY_MODULES = {"PIL": "Pillow"}


def ensure_dependencies() -> None:
    """Install required third-party packages if they are missing."""

    missing: List[str] = []
    for module_name, package_name in DEPENDENCY_MODULES.items():
        if importlib.util.find_spec(module_name) is None:
            missing.append(package_name)

    if not missing:
        return

    print("Missing dependencies detected, attempting installation:", ", ".join(missing))
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *missing],
            check=True,
        )
    except Exception as exc:  # pragma: no cover - best effort installer
        raise RuntimeError(
            "Failed to install required packages automatically. "
            "Please install them manually and retry."
        ) from exc


ensure_dependencies()

from PIL import Image, ImageColor, ImageOps
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

# ----------------------------
# Constants & Configuration
# ----------------------------

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
TRANSPARENT_FORMATS = {"PNG", "WEBP"}
DEFAULT_OUTPUT_DIRNAME = "output"
DEFAULT_PREVIEW_SUBDIR = "_preview"

INTERPOLATION_MAP = {
    "LANCZOS": Image.Resampling.LANCZOS,
    "BILINEAR": Image.Resampling.BILINEAR,
    "NEAREST": Image.Resampling.NEAREST,
}

ANCHOR_MAP = {
    "center": (0.5, 0.5),
    "top": (0.5, 0.0),
    "bottom": (0.5, 1.0),
    "left": (0.0, 0.5),
    "right": (1.0, 0.5),
    "top-left": (0.0, 0.0),
    "top-right": (1.0, 0.0),
    "bottom-left": (0.0, 1.0),
    "bottom-right": (1.0, 1.0),
}

PRESET_RATIOS = {
    "1:1": (1, 1),
    "4:3": (4, 3),
    "3:2": (3, 2),
    "16:9": (16, 9),
}

PRESET_MODES = {
    "长边 1080": ("long", 1080),
    "长边 2048": ("long", 2048),
    "短边 512": ("short", 512),
}

@dataclasses.dataclass
class ResizeConfig:
    input_root: Path
    output_root: Path
    mode: str = "long"  # long, short, box
    target_pixels: int = 2048
    target_width: int = 1024
    target_height: int = 1024
    interpolation: str = "LANCZOS"
    force_format: Optional[str] = None  # JPG/PNG/WEBP
    jpeg_quality: int = 90
    png_compress_level: int = 6
    webp_quality: int = 90
    remove_exif: bool = True
    ratios: List[Tuple[int, int]] = dataclasses.field(default_factory=list)
    strategy: str = "crop"  # crop or pad
    anchor: str = "center"
    background_color: str = "#000000"
    prefer_transparent: bool = True
    allow_overwrite: bool = True
    max_workers: Optional[int] = None
    force_ratio: bool = False

    def effective_workers(self) -> int:
        if self.max_workers:
            return max(1, self.max_workers)
        cpu = os.cpu_count() or 1
        return min(32, cpu * 2)


# ----------------------------
# Utility helpers
# ----------------------------

def human_time(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m{sec:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h{int(minutes)}m"


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def config_to_dict(config: ResizeConfig) -> Dict[str, object]:
    data = dataclasses.asdict(config)
    data["input_root"] = str(config.input_root)
    data["output_root"] = str(config.output_root)
    data["ratios"] = [f"{w}:{h}" for w, h in config.ratios]
    return data


def parse_ratio_text(ratio_text: str) -> Optional[Tuple[int, int]]:
    if not ratio_text:
        return None
    try:
        parts = ratio_text.replace("：", ":").split(":")
        if len(parts) != 2:
            return None
        w, h = int(parts[0]), int(parts[1])
        if w <= 0 or h <= 0:
            return None
        return (w, h)
    except ValueError:
        return None


def pick_anchor(anchor: str) -> Tuple[float, float]:
    return ANCHOR_MAP.get(anchor, (0.5, 0.5))


def has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)


def apply_exif_orientation(image: Image.Image) -> Image.Image:
    try:
        return ImageOps.exif_transpose(image)
    except Exception:
        return image


def compute_resize_size(image: Image.Image, config: ResizeConfig) -> Tuple[int, int]:
    width, height = image.size
    mode = config.mode
    if mode == "long":
        target = config.target_pixels
        long_side = max(width, height)
        if long_side == 0:
            return width, height
        scale = target / long_side
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))
    if mode == "short":
        target = config.target_pixels
        short_side = min(width, height)
        if short_side == 0:
            return width, height
        scale = target / short_side
        return max(1, int(round(width * scale))), max(1, int(round(height * scale)))
    if mode == "box":
        return config.target_width, config.target_height
    return width, height


def crop_to_ratio(image: Image.Image, ratio: Tuple[int, int], anchor: str) -> Image.Image:
    width, height = image.size
    target_ratio = ratio[0] / ratio[1]
    current_ratio = width / height
    ax, ay = pick_anchor(anchor)
    if current_ratio > target_ratio:
        # too wide -> crop width
        new_width = int(round(height * target_ratio))
        if new_width <= 0:
            return image
        left = int(round((width - new_width) * ax))
        left = max(0, min(left, width - new_width))
        box = (left, 0, left + new_width, height)
    else:
        # too tall -> crop height
        new_height = int(round(width / target_ratio))
        if new_height <= 0:
            return image
        top = int(round((height - new_height) * ay))
        top = max(0, min(top, height - new_height))
        box = (0, top, width, top + new_height)
    return image.crop(box)


def pad_to_ratio(image: Image.Image, ratio: Tuple[int, int], anchor: str, background: Tuple[int, int, int, int]) -> Image.Image:
    width, height = image.size
    target_ratio = ratio[0] / ratio[1]
    current_ratio = width / height
    if abs(current_ratio - target_ratio) < 1e-3:
        return image
    if current_ratio > target_ratio:
        # need more height
        new_height = int(round(width / target_ratio))
        new_width = width
    else:
        new_width = int(round(height * target_ratio))
        new_height = height
    canvas = Image.new("RGBA", (new_width, new_height), background)
    ax, ay = pick_anchor(anchor)
    left = int(round((new_width - width) * ax))
    top = int(round((new_height - height) * ay))
    canvas.paste(image, (left, top))
    return canvas


def prepare_canvas(color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    try:
        rgb = ImageColor.getrgb(color)
    except ValueError:
        rgb = (0, 0, 0)
    return (*rgb, alpha)


def convert_background(image: Image.Image, background: Tuple[int, int, int, int]) -> Image.Image:
    if image.mode in ("RGB", "L"):
        return image
    if image.mode in ("RGBA", "LA"):
        bg = Image.new("RGBA", image.size, background)
        bg.paste(image, mask=image.split()[-1])
        return bg.convert("RGB")
    if image.mode == "P":
        return image.convert("RGBA")
    return image.convert("RGB")


def normalize_format(ext: Optional[str]) -> Optional[str]:
    if not ext:
        return None
    ext = ext.strip().lower()
    if ext in {"jpeg", "jpg"}:
        return "JPEG"
    if ext == "png":
        return "PNG"
    if ext == "webp":
        return "WEBP"
    return ext.upper()


def determine_output_format(input_path: Path, config: ResizeConfig, image: Image.Image) -> Tuple[str, str]:
    original_ext = input_path.suffix.lower().strip(".")
    target_format = config.force_format or normalize_format(original_ext)
    if not target_format:
        target_format = "PNG"
    if config.prefer_transparent and has_alpha(image) and target_format == "JPEG":
        target_format = "PNG"
    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "TIFF": ".tif"}
    suffix = ext_map.get(target_format, f".{target_format.lower()}")
    return target_format, suffix


@dataclasses.dataclass
class ProcessResult:
    path: Path
    success: bool
    error: Optional[str] = None


class ProcessingCore:
    def __init__(self, config: ResizeConfig, logger: logging.Logger, stop_event: threading.Event):
        self.config = config
        self.logger = logger
        self.stop_event = stop_event

    def scan_files(self) -> List[Path]:
        files: List[Path] = []
        for root, dirs, filenames in os.walk(self.config.input_root):
            root_path = Path(root)
            # ignore hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('~')]
            for name in filenames:
                if name.startswith('.'):
                    continue
                path = root_path / name
                if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                    files.append(path)
                else:
                    self.logger.info("跳过非图片文件: %s", path)
        files.sort()
        return files

    def process_images(self, files: Sequence[Path], progress_callback=None) -> Tuple[int, int, float]:
        start = time.time()
        total = len(files)
        success = 0
        failure = 0
        if total == 0:
            return 0, 0, 0.0

        worker_count = self.config.effective_workers()
        self.logger.info("处理开始 - 线程: %s", worker_count)
        self.logger.info("任务总数: %s", total)
        self.logger.info("参数: %s", json.dumps(config_to_dict(self.config), ensure_ascii=False))

        ensure_directory(self.config.output_root)
        success_paths: List[str] = []
        failures: List[Tuple[str, str]] = []

        def task(path: Path) -> ProcessResult:
            if self.stop_event.is_set():
                return ProcessResult(path=path, success=False, error="Stopped")
            try:
                rel = path.relative_to(self.config.input_root)
                out_dir = self.config.output_root / rel.parent
                ensure_directory(out_dir)
                with Image.open(path) as img:
                    exif_data = img.info.get("exif")
                    img = apply_exif_orientation(img)
                    icc_profile = img.info.get("icc_profile")
                    processed, save_kwargs = self.process_single(img, path)
                    fmt, suffix = determine_output_format(path, self.config, processed)
                    out_path = out_dir / (path.stem + suffix)
                    if not self.config.allow_overwrite and out_path.exists():
                        return ProcessResult(path=path, success=False, error="Exists")
                    if fmt == "JPEG" and processed.mode in ("RGBA", "LA"):
                        background = prepare_canvas(self.config.background_color)
                        processed = convert_background(processed, background)
                    if icc_profile:
                        save_kwargs.setdefault("icc_profile", icc_profile)
                    if not self.config.remove_exif and exif_data:
                        save_kwargs.setdefault("exif", exif_data)
                    if self.config.remove_exif:
                        save_kwargs.pop("exif", None)
                    processed.save(out_path, fmt, **save_kwargs)
                return ProcessResult(path=path, success=True)
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Failed to process %s: %s", path, exc)
                return ProcessResult(path=path, success=False, error=str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {executor.submit(task, path): path for path in files}
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result.success:
                    success += 1
                    success_paths.append(str(result.path))
                else:
                    if result.error != "Stopped":
                        failure += 1
                        failures.append((str(result.path), result.error or "未知错误"))
                if progress_callback:
                    progress_callback(result)
                if self.stop_event.is_set():
                    break

        elapsed = time.time() - start
        self.logger.info("处理结束: 成功=%s 失败=%s 耗时=%s", success, failure, human_time(elapsed))
        if success_paths:
            self.logger.info("成功文件列表:")
            for item in success_paths:
                self.logger.info("SUCCESS %s", item)
        if failures:
            self.logger.info("失败文件列表:")
            for item, err in failures:
                self.logger.info("FAIL %s -> %s", item, err)
        return success, failure, elapsed

    def process_single(self, image: Image.Image, path: Path) -> Tuple[Image.Image, Dict]:
        config = self.config
        mode = image.mode
        background_color = prepare_canvas(config.background_color)
        ratios = config.ratios if config.force_ratio else []
        if config.mode == "box":
            target_size = (config.target_width, config.target_height)
        else:
            target_size = compute_resize_size(image, config)

        if config.mode == "box":
            if image.mode not in ("RGB", "RGBA", "L", "LA"):
                image = image.convert("RGBA" if has_alpha(image) else "RGB")
            else:
                image = image.copy()
        else:
            resample = INTERPOLATION_MAP.get(config.interpolation, Image.Resampling.LANCZOS)
            image = image.resize(target_size, resample=resample)

        if config.mode == "box":
            resample = INTERPOLATION_MAP.get(config.interpolation, Image.Resampling.LANCZOS)
            scale_w = target_size[0] / image.width
            scale_h = target_size[1] / image.height
            scale = max(scale_w, scale_h) if config.strategy == "crop" else min(scale_w, scale_h)
            new_size = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
            image = image.resize(new_size, resample=resample)
            if config.strategy == "crop":
                image = crop_fixed_box(image, target_size, config.anchor)
            else:
                image = pad_fixed_box(image, target_size, config.anchor, background_color)
        elif ratios:
            ratio = ratios[0]
            if config.strategy == "crop":
                image = crop_to_ratio(image, ratio, config.anchor)
            else:
                image = pad_to_ratio(image, ratio, config.anchor, background_color)

        save_kwargs: Dict = {}
        fmt, _ = determine_output_format(path, config, image)
        if fmt == "JPEG":
            save_kwargs["quality"] = config.jpeg_quality
            save_kwargs["optimize"] = True
        elif fmt == "PNG":
            save_kwargs["compress_level"] = config.png_compress_level
        elif fmt == "WEBP":
            save_kwargs["quality"] = config.webp_quality
        return image, save_kwargs


def crop_fixed_box(image: Image.Image, target_size: Tuple[int, int], anchor: str) -> Image.Image:
    width, height = image.size
    target_w, target_h = target_size
    ax, ay = pick_anchor(anchor)
    left = int(round((width - target_w) * ax))
    top = int(round((height - target_h) * ay))
    left = max(0, min(left, width - target_w))
    top = max(0, min(top, height - target_h))
    return image.crop((left, top, left + target_w, top + target_h))


def pad_fixed_box(image: Image.Image, target_size: Tuple[int, int], anchor: str, background: Tuple[int, int, int, int]) -> Image.Image:
    target_w, target_h = target_size
    canvas = Image.new("RGBA", (target_w, target_h), background)
    ax, ay = pick_anchor(anchor)
    left = int(round((target_w - image.width) * ax))
    top = int(round((target_h - image.height) * ay))
    canvas.paste(image, (left, top), image if has_alpha(image) else None)
    return canvas


# ----------------------------
# Logging utilities
# ----------------------------

def setup_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"processor-{id(log_path)}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger


# ----------------------------
# GUI Implementation
# ----------------------------

class ImageBatchToolApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image Batch Resize Tool")
        self.geometry("960x640")
        self.resizable(True, True)

        self.stop_event = threading.Event()
        self.progress_queue: "queue.Queue[ProcessResult]" = queue.Queue()
        self.processor_thread: Optional[threading.Thread] = None
        self.files_to_process: List[Path] = []
        self.logger: Optional[logging.Logger] = None
        self.start_time: Optional[float] = None
        self.processor: Optional[ProcessingCore] = None

        self.create_widgets()
        self.after(200, self.process_queue)

    def create_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        main_frame = ttk.Frame(self)
        main_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        for i in range(3):
            main_frame.rowconfigure(i, weight=0)
        main_frame.rowconfigure(3, weight=1)
        main_frame.columnconfigure(0, weight=1)

        # Directory selectors
        dir_frame = ttk.LabelFrame(main_frame, text="目录")
        dir_frame.grid(row=0, column=0, sticky="ew")
        dir_frame.columnconfigure(1, weight=1)

        ttk.Label(dir_frame, text="输入目录").grid(row=0, column=0, padx=5, pady=5)
        self.input_var = tk.StringVar()
        input_entry = ttk.Entry(dir_frame, textvariable=self.input_var)
        input_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        ttk.Button(dir_frame, text="选择", command=self.choose_input).grid(row=0, column=2, padx=5, pady=5)

        ttk.Label(dir_frame, text="输出目录").grid(row=1, column=0, padx=5, pady=5)
        self.output_var = tk.StringVar()
        output_entry = ttk.Entry(dir_frame, textvariable=self.output_var)
        output_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        ttk.Button(dir_frame, text="选择", command=self.choose_output).grid(row=1, column=2, padx=5, pady=5)
        ttk.Label(dir_frame, text="[默认同级 output]").grid(row=1, column=3, padx=5, pady=5)

        # Options frame
        options_frame = ttk.Frame(main_frame)
        options_frame.grid(row=1, column=0, sticky="nsew", pady=10)
        options_frame.columnconfigure(0, weight=1)
        options_frame.columnconfigure(1, weight=1)

        left = ttk.LabelFrame(options_frame, text="缩放设置")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left.columnconfigure(1, weight=1)

        self.mode_var = tk.StringVar(value="long")
        ttk.Radiobutton(left, text="长边定像素", variable=self.mode_var, value="long").grid(row=0, column=0, sticky="w", padx=5, pady=2, columnspan=2)
        ttk.Radiobutton(left, text="短边定像素", variable=self.mode_var, value="short").grid(row=1, column=0, sticky="w", padx=5, pady=2, columnspan=2)
        ttk.Radiobutton(left, text="目标宽×高", variable=self.mode_var, value="box").grid(row=2, column=0, sticky="w", padx=5, pady=2, columnspan=2)

        ttk.Label(left, text="像素/长边").grid(row=3, column=0, padx=5, pady=2)
        self.pixel_var = tk.IntVar(value=2048)
        ttk.Entry(left, textvariable=self.pixel_var, width=10).grid(row=3, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="目标宽").grid(row=4, column=0, padx=5, pady=2)
        self.width_var = tk.IntVar(value=1024)
        ttk.Entry(left, textvariable=self.width_var, width=10).grid(row=4, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="目标高").grid(row=5, column=0, padx=5, pady=2)
        self.height_var = tk.IntVar(value=1024)
        ttk.Entry(left, textvariable=self.height_var, width=10).grid(row=5, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="插值算法").grid(row=6, column=0, padx=5, pady=2)
        self.interp_var = tk.StringVar(value="LANCZOS")
        interp_combo = ttk.Combobox(left, textvariable=self.interp_var, values=list(INTERPOLATION_MAP.keys()), state="readonly")
        interp_combo.grid(row=6, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="常用预设").grid(row=7, column=0, padx=5, pady=2)
        self.preset_var = tk.StringVar(value="")
        preset_combo = ttk.Combobox(left, textvariable=self.preset_var, values=list(PRESET_MODES.keys()), state="readonly")
        preset_combo.grid(row=7, column=1, padx=5, pady=2, sticky="ew")
        preset_combo.bind("<<ComboboxSelected>>", self.apply_mode_preset)

        ttk.Label(left, text="输出格式").grid(row=8, column=0, padx=5, pady=2)
        self.format_var = tk.StringVar(value="原格式")
        format_combo = ttk.Combobox(left, textvariable=self.format_var, values=["原格式", "JPEG", "PNG", "WEBP"], state="readonly")
        format_combo.grid(row=8, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="JPEG质量").grid(row=9, column=0, padx=5, pady=2)
        self.jpeg_var = tk.IntVar(value=90)
        ttk.Scale(left, from_=60, to=95, orient="horizontal", variable=self.jpeg_var).grid(row=9, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="PNG压缩").grid(row=10, column=0, padx=5, pady=2)
        self.png_var = tk.IntVar(value=6)
        ttk.Scale(left, from_=0, to=9, orient="horizontal", variable=self.png_var).grid(row=10, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(left, text="WebP质量").grid(row=11, column=0, padx=5, pady=2)
        self.webp_var = tk.IntVar(value=90)
        ttk.Scale(left, from_=60, to=100, orient="horizontal", variable=self.webp_var).grid(row=11, column=1, padx=5, pady=2, sticky="ew")

        right = ttk.LabelFrame(options_frame, text="比例与策略")
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right.columnconfigure(0, weight=1)
        right.columnconfigure(1, weight=1)

        ttk.Label(right, text="常用比例").grid(row=0, column=0, padx=5, pady=2)
        self.ratio_listbox = tk.Listbox(right, selectmode=tk.MULTIPLE, height=4)
        for preset in PRESET_RATIOS.keys():
            self.ratio_listbox.insert(tk.END, preset)
        self.ratio_listbox.grid(row=1, column=0, padx=5, pady=2, sticky="nsew")

        ttk.Label(right, text="自定义比例").grid(row=0, column=1, padx=5, pady=2)
        self.custom_ratio_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.custom_ratio_var).grid(row=1, column=1, padx=5, pady=2, sticky="ew")

        self.force_ratio_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="启用强制比例", variable=self.force_ratio_var).grid(row=2, column=0, columnspan=2, padx=5, pady=2, sticky="w")

        ttk.Label(right, text="策略").grid(row=3, column=0, padx=5, pady=2)
        self.strategy_var = tk.StringVar(value="crop")
        ttk.Combobox(right, textvariable=self.strategy_var, values=["crop", "pad"], state="readonly").grid(row=3, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(right, text="锚点").grid(row=4, column=0, padx=5, pady=2)
        self.anchor_var = tk.StringVar(value="center")
        ttk.Combobox(right, textvariable=self.anchor_var, values=list(ANCHOR_MAP.keys()), state="readonly").grid(row=4, column=1, padx=5, pady=2, sticky="ew")

        ttk.Label(right, text="背景颜色").grid(row=5, column=0, padx=5, pady=2)
        self.bg_var = tk.StringVar(value="#000000")
        bg_entry = ttk.Entry(right, textvariable=self.bg_var)
        bg_entry.grid(row=5, column=1, padx=5, pady=2, sticky="ew")
        ttk.Button(right, text="选择颜色", command=self.choose_color).grid(row=6, column=1, padx=5, pady=2, sticky="ew")

        self.exif_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="保留EXIF", variable=self.exif_var).grid(row=7, column=0, padx=5, pady=2, sticky="w")

        self.transparent_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="透明优先", variable=self.transparent_var).grid(row=7, column=1, padx=5, pady=2, sticky="w")

        ttk.Label(right, text="并发度").grid(row=8, column=0, padx=5, pady=2)
        self.thread_var = tk.IntVar(value=0)
        ttk.Entry(right, textvariable=self.thread_var).grid(row=8, column=1, padx=5, pady=2, sticky="ew")

        self.overwrite_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(right, text="允许覆盖", variable=self.overwrite_var).grid(row=9, column=0, columnspan=2, padx=5, pady=2, sticky="w")

        # Bottom buttons
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=2, column=0, sticky="ew", pady=5)
        for i in range(6):
            btn_frame.columnconfigure(i, weight=1)

        ttk.Button(btn_frame, text="加载预设", command=self.load_preset).grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="保存预设", command=self.save_preset).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="扫描", command=self.scan_files_action).grid(row=0, column=2, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="预览9张", command=self.preview_action).grid(row=0, column=3, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="开始处理", command=self.start_processing).grid(row=0, column=4, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="停止", command=self.stop_processing).grid(row=0, column=5, padx=5, pady=5, sticky="ew")
        ttk.Button(btn_frame, text="打开输出目录", command=self.open_output).grid(row=0, column=6, padx=5, pady=5, sticky="ew")

        # Progress area
        progress_frame = ttk.Frame(main_frame)
        progress_frame.grid(row=3, column=0, sticky="nsew")
        progress_frame.columnconfigure(0, weight=1)
        progress_frame.rowconfigure(2, weight=1)

        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=5, pady=5)

        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(progress_frame, textvariable=self.status_var).grid(row=1, column=0, sticky="w", padx=5)

        self.log_text = tk.Text(progress_frame, height=12)
        self.log_text.grid(row=2, column=0, sticky="nsew", padx=5, pady=5)
        scrollbar = ttk.Scrollbar(progress_frame, command=self.log_text.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def choose_input(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.input_var.set(path)
            default_output = Path(path).parent / DEFAULT_OUTPUT_DIRNAME
            if not self.output_var.get():
                self.output_var.set(str(default_output))

    def choose_output(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.output_var.set(path)

    def choose_color(self) -> None:
        color = colorchooser.askcolor(color=self.bg_var.get())
        if color and color[1]:
            self.bg_var.set(color[1])

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def gather_config(self) -> Optional[ResizeConfig]:
        input_path = Path(self.input_var.get()) if self.input_var.get() else None
        output_path = Path(self.output_var.get()) if self.output_var.get() else None
        if not input_path or not input_path.exists():
            messagebox.showwarning("提示", "请选择有效的输入目录")
            return None
        if not output_path:
            output_path = input_path.parent / DEFAULT_OUTPUT_DIRNAME
            self.output_var.set(str(output_path))
        ratios = []
        for idx in self.ratio_listbox.curselection():
            label = self.ratio_listbox.get(idx)
            ratios.append(PRESET_RATIOS[label])
        custom = parse_ratio_text(self.custom_ratio_var.get())
        if custom:
            ratios.append(custom)
        return ResizeConfig(
            input_root=input_path,
            output_root=output_path,
            mode=self.mode_var.get(),
            target_pixels=self.pixel_var.get(),
            target_width=self.width_var.get(),
            target_height=self.height_var.get(),
            interpolation=self.interp_var.get(),
            force_format=None if self.format_var.get() == "原格式" else self.format_var.get(),
            jpeg_quality=int(self.jpeg_var.get()),
            png_compress_level=int(self.png_var.get()),
            webp_quality=int(self.webp_var.get()),
            remove_exif=not self.exif_var.get(),
            ratios=ratios,
            strategy=self.strategy_var.get(),
            anchor=self.anchor_var.get(),
            background_color=self.bg_var.get(),
            prefer_transparent=self.transparent_var.get(),
            allow_overwrite=self.overwrite_var.get(),
            max_workers=self.thread_var.get() or None,
            force_ratio=self.force_ratio_var.get(),
        )

    def scan_files_action(self) -> None:
        config = self.gather_config()
        if not config:
            return
        self.logger = setup_logger(config.output_root / "process.log")
        processor = ProcessingCore(config, self.logger, self.stop_event)
        files = processor.scan_files()
        self.files_to_process = files
        self.log(f"发现 {len(files)} 个待处理文件")
        self.status_var.set(f"待处理: {len(files)}")
        self.progress.configure(maximum=max(1, len(files)))
        self.progress['value'] = 0

    def start_processing(self) -> None:
        if self.processor_thread and self.processor_thread.is_alive():
            messagebox.showinfo("提示", "处理正在进行中")
            return
        config = self.gather_config()
        if not config:
            return
        if not self.files_to_process:
            self.scan_files_action()
            if not self.files_to_process:
                messagebox.showinfo("提示", "没有找到需要处理的文件")
                return
        self.stop_event.clear()
        self.logger = setup_logger(config.output_root / "process.log")
        self.processor = ProcessingCore(config, self.logger, self.stop_event)
        self.progress['value'] = 0
        self.progress.configure(maximum=max(1, len(self.files_to_process)))
        self.start_time = time.time()
        self.status_var.set("处理中...")
        self.log("开始处理")
        self.processor_thread = threading.Thread(target=self.run_processing, daemon=True)
        self.processor_thread.start()

    def run_processing(self) -> None:
        assert self.processor is not None
        success, failure, elapsed = self.processor.process_images(
            self.files_to_process,
            progress_callback=self.progress_queue.put,
        )
        if self.stop_event.is_set():
            self.log("处理已停止")
            self.status_var.set("已停止")
        else:
            self.log(f"完成: 成功 {success} 失败 {failure} 用时 {human_time(elapsed)}")
            self.status_var.set(f"完成: {success} 成功, {failure} 失败, 耗时 {human_time(elapsed)}")
        self.files_to_process = []

    def stop_processing(self) -> None:
        if self.processor_thread and self.processor_thread.is_alive():
            self.stop_event.set()
            self.log("停止请求已发送")
        else:
            self.log("没有正在运行的任务")

    def open_output(self) -> None:
        path = self.output_var.get()
        if not path:
            messagebox.showinfo("提示", "请先设置输出目录")
            return
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("提示", f"输出目录: {path}")
        except FileNotFoundError:
            messagebox.showwarning("提示", "输出目录不存在")

    def process_queue(self) -> None:
        try:
            while True:
                result = self.progress_queue.get_nowait()
                if result.error and result.error != "Stopped":
                    self.log(f"失败: {result.path} ({result.error})")
                elif result.success:
                    self.log(f"成功: {result.path}")
                self.progress.step(1)
        except queue.Empty:
            pass
        self.after(200, self.process_queue)

    def preview_action(self) -> None:
        config = self.gather_config()
        if not config:
            return
        processor = ProcessingCore(config, setup_logger(config.output_root / "process.log"), self.stop_event)
        files = processor.scan_files()
        preview_dir = config.output_root / DEFAULT_PREVIEW_SUBDIR
        ensure_directory(preview_dir)
        for path in files[:9]:
            rel = path.relative_to(config.input_root)
            out_path = preview_dir / (path.stem + "_preview" + path.suffix)
            with Image.open(path) as img:
                img = apply_exif_orientation(img)
                processed, save_kwargs = processor.process_single(img, path)
                fmt, suffix = determine_output_format(path, config, processed)
                out_path = out_path.with_suffix(suffix)
                processed.save(out_path, fmt, **save_kwargs)
        self.log("预览图片已生成到 _preview 目录")

    def load_preset(self) -> None:
        path = filedialog.askopenfilename(title="选择预设", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:  # pylint: disable=broad-except
            messagebox.showerror("错误", f"读取预设失败: {exc}")
            return
        self.apply_preset(data)
        self.log(f"已加载预设 {path}")

    def save_preset(self) -> None:
        config = self.gather_config()
        if not config:
            return
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = dataclasses.asdict(config)
        data["input_root"] = str(config.input_root)
        data["output_root"] = str(config.output_root)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.log(f"预设已保存到 {path}")

    def apply_preset(self, data: Dict) -> None:
        self.input_var.set(data.get("input_root", ""))
        self.output_var.set(data.get("output_root", ""))
        self.mode_var.set(data.get("mode", "long"))
        self.pixel_var.set(data.get("target_pixels", 2048))
        self.width_var.set(data.get("target_width", 1024))
        self.height_var.set(data.get("target_height", 1024))
        self.interp_var.set(data.get("interpolation", "LANCZOS"))
        force_format = data.get("force_format")
        self.format_var.set(force_format if force_format else "原格式")
        self.jpeg_var.set(data.get("jpeg_quality", 90))
        self.png_var.set(data.get("png_compress_level", 6))
        self.webp_var.set(data.get("webp_quality", 90))
        self.exif_var.set(not data.get("remove_exif", True))
        self.strategy_var.set(data.get("strategy", "crop"))
        self.anchor_var.set(data.get("anchor", "center"))
        self.bg_var.set(data.get("background_color", "#000000"))
        self.transparent_var.set(data.get("prefer_transparent", True))
        self.overwrite_var.set(data.get("allow_overwrite", True))
        self.thread_var.set(data.get("max_workers", 0) or 0)
        self.force_ratio_var.set(data.get("force_ratio", False))
        ratios = data.get("ratios", [])
        self.ratio_listbox.selection_clear(0, tk.END)
        for i, preset in enumerate(PRESET_RATIOS.values()):
            if preset in ratios:
                self.ratio_listbox.selection_set(i)
        custom_ratios = [r for r in ratios if r not in PRESET_RATIOS.values()]
        if custom_ratios:
            self.custom_ratio_var.set(f"{custom_ratios[0][0]}:{custom_ratios[0][1]}")
        else:
            self.custom_ratio_var.set("")

    def apply_mode_preset(self, _event=None) -> None:
        preset = self.preset_var.get()
        if preset in PRESET_MODES:
            mode, pixels = PRESET_MODES[preset]
            self.mode_var.set(mode)
            self.pixel_var.set(pixels)


# ----------------------------
# CLI handling
# ----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量缩放与剪裁图片，并按原目录结构输出")
    parser.add_argument("--input", type=Path, help="输入根目录")
    parser.add_argument("--output", type=Path, help="输出根目录", default=None)
    parser.add_argument("--mode", choices=["long", "short", "box"], default="long")
    parser.add_argument("--pixels", type=int, default=2048, help="长边或短边像素")
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--ratio", action="append", default=[], help="目标比例 如 16:9 可多次指定")
    parser.add_argument("--force-ratio", action="store_true", help="启用强制比例")
    parser.add_argument("--strategy", choices=["crop", "pad"], default="crop")
    parser.add_argument("--anchor", choices=list(ANCHOR_MAP.keys()), default="center")
    parser.add_argument("--format", choices=["jpeg", "png", "webp"], default=None)
    parser.add_argument("--quality", type=int, default=90, help="JPEG/WebP 质量")
    parser.add_argument("--png-level", type=int, default=6)
    parser.add_argument("--keep-exif", action="store_true")
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--background", default="#000000")
    parser.add_argument("--no-transparent-priority", action="store_true")
    parser.add_argument("--no-overwrite", action="store_true")
    parser.add_argument("--gui", action="store_true", help="启动 GUI")
    return parser


def run_cli(args: argparse.Namespace) -> None:
    if args.gui or not args.input:
        app = ImageBatchToolApp()
        app.mainloop()
        return
    input_root = args.input
    if not input_root.exists():
        raise SystemExit("输入目录不存在")
    output_root = args.output or (input_root.parent / DEFAULT_OUTPUT_DIRNAME)
    ratios = [parse_ratio_text(r) for r in args.ratio]
    ratios = [r for r in ratios if r]
    config = ResizeConfig(
        input_root=input_root,
        output_root=output_root,
        mode=args.mode,
        target_pixels=args.pixels,
        target_width=args.width,
        target_height=args.height,
        interpolation="LANCZOS",
        force_format=normalize_format(args.format) if args.format else None,
        jpeg_quality=args.quality,
        png_compress_level=args.png_level,
        webp_quality=args.quality,
        remove_exif=not args.keep_exif,
        ratios=ratios,
        strategy=args.strategy,
        anchor=args.anchor,
        background_color=args.background,
        prefer_transparent=not args.no_transparent_priority,
        allow_overwrite=not args.no_overwrite,
        max_workers=args.threads or None,
        force_ratio=args.force_ratio,
    )
    ensure_directory(output_root)
    logger = setup_logger(output_root / "process.log")
    stop_event = threading.Event()
    processor = ProcessingCore(config, logger, stop_event)
    files = processor.scan_files()
    logger.info("扫描到 %s 个文件", len(files))
    success, failure, elapsed = processor.process_images(files)
    logger.info("完成: 成功=%s 失败=%s 耗时=%s", success, failure, human_time(elapsed))


# ----------------------------
# Self Checks
# ----------------------------

def _self_check() -> None:
    img = Image.new("RGB", (400, 200), "red")
    cropped = crop_to_ratio(img, (1, 1), "center")
    assert cropped.size == (200, 200)
    padded = pad_to_ratio(img, (1, 1), "center", (0, 0, 0, 255))
    assert padded.size[0] == padded.size[1]
    box = Image.new("RGB", (500, 300), "blue")
    cropped_box = crop_fixed_box(box, (300, 300), "center")
    assert cropped_box.size == (300, 300)


if __name__ == "__main__":
    _self_check()
    parser = build_arg_parser()
    run_cli(parser.parse_args())
