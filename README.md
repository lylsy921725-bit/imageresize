# Image Batch Resize Tool

一个基于 Python 3.11+ 与 Tkinter 的轻量级批量图片缩放与剪裁工具，提供桌面 GUI 与 CLI 两种使用方式，可通过 PyInstaller 打包为单个 Windows 可执行文件。

> **截图占位**：请在实际使用后替换此处为 GUI 截图。

## 功能特性
- 递归扫描输入根目录，镜像输出目录结构，记录非图片文件并跳过。
- 支持 JPEG/PNG/WebP/BMP/TIFF 等常见格式，自动识别透明通道并处理。
- EXIF 自动旋转，支持保留或移除 EXIF 元数据；尽量保留 ICC Profile。
- 多种缩放模式：长边/短边定像素与目标宽×高，支持裁剪或填充策略与锚点位置。
- 比例控制：内置 1:1、4:3、3:2、16:9，可自定义输入并保存为预设 JSON。
- 输出格式可继承原图或强制转换为 JPEG/PNG/WebP，并可配置 JPEG 质量、PNG 压缩等级、WebP 质量。
- 多线程批处理，默认线程数 `min(32, CPU*2)`，可随时中止处理。
- 日志写入输出目录 `process.log`，包含参数、成功/失败文件列表及错误信息。
- 生成 `_preview` 目录的 3×3 预览图以快速确认配置。
- 启动时自动检测 Pillow 依赖，缺失时会尝试通过 `pip` 自动安装。

## 安装依赖
```bash
python -m venv .venv
. .venv/Scripts/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt  # 脚本会自动检测缺失依赖，但建议提前安装
```

## GUI 使用步骤
1. 运行 `python main.py --gui` 或直接双击打包后的可执行文件。
2. 选择输入目录与输出目录（默认在输入目录同级生成 `output` 文件夹）。
3. 配置缩放模式、像素或宽高、比例策略、输出格式、插值算法等参数。
4. 可选：加载或保存 JSON 预设、生成 9 张预览图确认效果。
5. 点击“扫描”查看待处理文件数量，再点击“开始处理”执行。
6. 进度条与日志窗口会实时显示处理状态，完成后可点击“打开输出目录”。

## CLI 使用示例
```bash
python main.py --input "D:\\photos" --output "D:\\photos_output" \
    --mode long --pixels 2048 --ratio 1:1 --force-ratio \
    --strategy crop --anchor center --format jpeg --quality 90 \
    --keep-exif --threads 16 --background "#202020"
```

常用 CLI 参数说明：
- `--mode {long,short,box}`：选择缩放模式。
- `--pixels`：长边或短边目标像素。
- `--width --height`：`box` 模式的目标宽高。
- `--ratio 16:9`：可多次指定多个比例；配合 `--force-ratio` 生效。
- `--strategy {crop,pad}`：裁剪或留边。
- `--anchor`：锚点，支持 `center/top/bottom/left/right/top-left/top-right/bottom-left/bottom-right`。
- `--format`：强制输出格式，`jpeg/png/webp`。
- `--quality` / `--png-level`：JPEG/WebP 质量与 PNG 压缩等级。
- `--keep-exif`：保留 EXIF；默认移除。
- `--threads`：线程数量，默认自动。
- `--no-transparent-priority`：允许强制格式覆盖透明通道。
- `--no-overwrite`：不覆盖同名文件。

## 日志与预览
- 输出目录将生成 `process.log`，记录开始/结束时间、参数摘要、成功/失败文件列表。
- “预览 9 张”按钮会在输出目录下创建 `_preview` 子目录，以当前设置生成 9 张示例。

## 常见问题
- **EXIF 方向不正确**：确保未勾选“保留 EXIF”，工具默认会旋转并移除原有方向信息。
- **透明背景丢失**：若强制输出 JPEG，请指定背景色；或开启“透明优先”自动切换到支持透明的格式。
- **JPEG 质量太低**：将 GUI 滑块或 CLI `--quality` 调整为 90 以上。
- **内存不足**：降低目标像素或线程数，必要时勾选“停止”按钮终止任务。
- **权限错误**：确认对输出目录拥有写权限，或将输出目录设置到其他位置。

## 打包为单个 exe
```bash
pip install -r requirements.txt
pyinstaller -F -w -n ImgBatchTool main.py
pyinstaller -F -n ImgBatchTool_console main.py
```

生成的 `dist/ImgBatchTool.exe` 为无控制台 GUI 版本，`dist/ImgBatchTool_console.exe` 适合 CLI 调试。

## 许可证
MIT License.
