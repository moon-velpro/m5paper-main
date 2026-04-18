from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class FlashPart:
    offset: str
    source: Path
    output_name: str


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_flashing_config(build_dir: Path) -> tuple[list[FlashPart], dict[str, Any]]:
    flasher_args_path = build_dir / "flasher_args.json"
    if flasher_args_path.exists():
        payload = json.loads(flasher_args_path.read_text(encoding="utf-8"))
        flash_files = payload.get("flash_files") or {}
        parts: list[FlashPart] = []
        for offset, relative_path in flash_files.items():
            source = (build_dir / relative_path).resolve()
            parts.append(
                FlashPart(
                    offset=offset.lower(),
                    source=source,
                    output_name=Path(relative_path).name,
                )
            )
        return parts, payload

    fallback = [
        FlashPart("0x0", build_dir / "bootloader" / "bootloader.bin", "bootloader.bin"),
        FlashPart("0x8000", build_dir / "partition_table" / "partition-table.bin", "partition-table.bin"),
        FlashPart("0x10000", build_dir / "m5paper_hello.bin", "m5paper_hello.bin"),
    ]
    payload = {
        "extra_esptool_args": {
            "chip": "esp32s3",
            "flash_mode": "dio",
            "flash_freq": "80m",
            "flash_size": "16MB",
        }
    }
    return fallback, payload


def normalize_parts(parts: list[FlashPart]) -> list[FlashPart]:
    normalized: list[FlashPart] = []
    for part in parts:
        normalized.append(
            FlashPart(
                offset=part.offset.lower(),
                source=part.source,
                output_name=part.output_name.replace("partition-table", "partition-table"),
            )
        )
    normalized.sort(key=lambda item: int(item.offset, 16))
    return normalized


def ensure_build_outputs(parts: list[FlashPart]) -> None:
    missing = [str(part.source) for part in parts if not part.source.exists()]
    if missing:
        joined = "\n".join(missing)
        raise SystemExit(f"缺少固件构建产物，请先完成编译。\n{joined}")


def build_manifest(version: str, parts: list[FlashPart], chip: str) -> dict[str, Any]:
    return {
        "name": "M5Paper 中文看板",
        "version": version,
        "new_install_prompt_erase": True,
        "builds": [
            {
                "chipFamily": chip.upper().replace("ESP32S3", "ESP32-S3"),
                "parts": [
                    {
                        "path": f"firmware/{part.output_name}",
                        "offset": int(part.offset, 16),
                    }
                    for part in parts
                ],
            }
        ],
    }


def batch_python_probe() -> str:
    return r"""set "PYTHON_CMD="
where py >nul 2>nul && set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
  where python >nul 2>nul && set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
  where python3 >nul 2>nul && set "PYTHON_CMD=python3"
)
if not defined PYTHON_CMD (
  echo.
  echo [错误] 未找到 Python。请安装 Python 3，或改用网页烧录方式。
  pause
  exit /b 1
)"""


def build_windows_flash_script(parts: list[FlashPart], chip: str, extra: dict[str, Any]) -> str:
    flash_mode = extra.get("flash_mode", "dio")
    flash_freq = extra.get("flash_freq", "80m")
    flash_size = extra.get("flash_size", "16MB")

    write_flash_parts = " ".join(
        f"{part.offset} firmware\\{part.output_name}"
        for part in parts
    )
    return f"""@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

echo ==========================================
echo M5Paper 中文看板 Windows 一键烧录
echo ==========================================
echo.
echo 如果设备处于深睡状态：
echo 1. 按住 Boot
echo 2. 短按 Reset
echo 3. 松开 Boot 后立刻继续烧录
echo.
echo 当前检测到的串口：
powershell -NoProfile -Command "Get-CimInstance Win32_SerialPort ^| Select-Object DeviceID,Name ^| Format-Table -AutoSize"
echo.
set /p PORT=请输入串口号（例如 COM5）:
if "%PORT%"=="" (
  echo [错误] 串口不能为空。
  pause
  exit /b 1
)

{batch_python_probe()}

echo.
echo [1/2] 检查 esptool...
cmd /c %PYTHON_CMD% -m esptool version >nul 2>nul
if errorlevel 1 (
  echo 未检测到 esptool，正在自动安装...
  cmd /c %PYTHON_CMD% -m pip install --user esptool
  if errorlevel 1 (
    echo [错误] esptool 安装失败。
    pause
    exit /b 1
  )
)

echo.
echo [2/2] 开始烧录，请勿断开 USB...
cmd /c %PYTHON_CMD% -m esptool --chip {chip} --port %PORT% --baud 460800 --before default_reset --after hard_reset write_flash --flash_mode {flash_mode} --flash_freq {flash_freq} --flash_size {flash_size} {write_flash_parts}
if errorlevel 1 (
  echo.
  echo [错误] 烧录失败，请确认：
  echo - 串口是否正确
  echo - USB 线是否支持数据
  echo - 是否已按 Boot + Reset 进入下载模式
  pause
  exit /b 1
)

echo.
echo 烧录完成。首次启动如果没有配置，会自动开启热点 M5Paper-Setup。
echo 手机连接后访问 http://192.168.4.1 即可填写 Wi-Fi 和 API_URL。
pause
"""


def build_package_readme(version: str, manifest_name: str) -> str:
    return f"""M5Paper 中文看板 固件包
版本：{version}

你收到这个压缩包后，只需要做下面几步：

一、Windows 一键烧录
1. 用 USB 数据线连接 M5Paper
2. 双击 flash_windows.bat
3. 输入串口号，例如 COM5
4. 等待烧录完成

如果设备处于深睡状态无法连接：
1. 按住 Boot
2. 短按 Reset
3. 松开 Boot 后立刻执行烧录

二、首次配网
1. 设备第一次没有配置时会自动开启热点：M5Paper-Setup
2. 手机连接热点后，浏览器打开：192.168.4.1
3. 填写 Wi-Fi、Wi-Fi 密码、API_URL、家庭地址、天气地址、路线起点、路线终点、刷新间隔
4. 保存后设备会自动重启

三、重新进入配置模式
- 开机时长按 A 键约 2 秒

四、如果你要做网页烧录
- manifest 文件：{manifest_name}
- 固件文件在 firmware 目录下
- 可用于 ESP Web Tools 或你自己的静态网页托管
"""


def copy_parts(parts: list[FlashPart], firmware_dir: Path) -> list[FlashPart]:
    copied: list[FlashPart] = []
    for part in parts:
        target = firmware_dir / part.output_name
        shutil.copy2(part.source, target)
        copied.append(FlashPart(offset=part.offset, source=target, output_name=part.output_name))
    return copied


def package_release(
    build_dir: Path,
    output_dir: Path,
    version: str,
    chip: str,
) -> Path:
    parts, payload = load_flashing_config(build_dir)
    parts = normalize_parts(parts)
    ensure_build_outputs(parts)

    extra_esptool_args = payload.get("extra_esptool_args") or {}
    chip_name = str(extra_esptool_args.get("chip") or chip)

    package_root = output_dir / f"m5paper-dashboard-{version}"
    firmware_dir = package_root / "firmware"
    if package_root.exists():
        shutil.rmtree(package_root)
    firmware_dir.mkdir(parents=True, exist_ok=True)

    copied_parts = copy_parts(parts, firmware_dir)
    manifest = build_manifest(version, copied_parts, chip_name)
    manifest_name = "flash_manifest.json"
    (package_root / manifest_name).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (package_root / "flash_windows.bat").write_text(
        build_windows_flash_script(copied_parts, chip_name, extra_esptool_args),
        encoding="utf-8",
        newline="\r\n",
    )
    (package_root / "README_先看我.txt").write_text(
        build_package_readme(version, manifest_name),
        encoding="utf-8",
    )
    return package_root


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="把 ESP-IDF 构建产物打包成可发布的 M5Paper 固件包。")
    parser.add_argument("--build-dir", type=Path, default=repo_root() / "m5paper_hw" / "build")
    parser.add_argument("--output-dir", type=Path, default=repo_root() / "release_dist")
    parser.add_argument("--version", default=datetime.now().strftime("%Y%m%d-%H%M"))
    parser.add_argument("--chip", default="esp32s3")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    package_root = package_release(
        build_dir=args.build_dir.resolve(),
        output_dir=args.output_dir.resolve(),
        version=args.version,
        chip=args.chip,
    )
    print(f"发布包已生成：{package_root}")


if __name__ == "__main__":
    main()
