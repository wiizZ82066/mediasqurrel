# BUILD_SIZE_REPORT — 安装包体积审计

## 优化前（基线 `4d9f478`）

| 指标 | 值 |
|---|---|
| 安装包 | 646 MB |
| backend（未压缩） | 1,831 MB |
| playwright 浏览器 | 1,386 MB（双 revision 1228+1234） |
| patchright 残留 | 88 MB（应急 fork，默认不加载） |

## 优化后

| 指标 | 值 | 变化 |
|---|---|---|
| 安装包 | **424 MB** | **-222 MB (-34%)** |
| backend（未压缩） | **1,058 MB** | -773 MB |
| playwright 浏览器 | **701 MB** | -685 MB（仅保留 revision 1234） |
| patchright | **0 MB** | -88 MB（后处理删除） |

## Top 20 大文件（优化后）

| # | 文件 | 大小 | 来源 | 必要性 |
|---|---|---|---|---|
| 1 | chromium-1234/chrome.dll | 284 MB | Playwright | ✅ 下载自动化核心 |
| 2 | chromium_headless_shell-1234/chrome-headless-shell.exe | 201 MB | Playwright | ✅ 无头模式 |
| 3 | playwright/driver/node.exe | 88 MB | Playwright driver | ✅ 协议桥接 |
| 4 | cv2.pyd | 82 MB | OpenCV | ✅ 缩略图+人脸检测 |
| 5 | opencv_videoio_ffmpeg500_64.dll | 29 MB | OpenCV | ✅ 视频抽帧 |
| 6 | chromium-1234/dxcompiler.dll | 25 MB | Playwright | ✅ 渲染依赖 |
| 7 | MediaSquirrelBackend.exe | 22 MB | PyInstaller | ✅ 后端入口 |
| 8 | chromium-1234/resources.pak | 20 MB | Playwright | ✅ UI 资源 |
| 9 | libscipy_openblas64.dll | 20 MB | NumPy | ✅ OpenCV 矩阵运算 |
| 10 | icudtl.dat (chromium×2) | 21 MB | Playwright | ✅ 国际化 |
| 11 | _rust.pyd (cryptography) | 9 MB | cryptography | ✅ TLS |
| 12 | _avif.pyd (PIL) | 8 MB | Pillow | ✅ 图片格式 |
| 13 | python314.dll | 6 MB | Python runtime | ✅ |
| 14 | vk_swiftshader.dll ×2 | 10 MB | Playwright | ✅ GPU 软渲染 |
| 15 | _pydantic_core.pyd | 5 MB | Pydantic | ✅ FastAPI |
| 16 | libcrypto-3.dll | 5 MB | OpenSSL | ✅ TLS |

## 删除依据

| 删除项 | 引用检查 | 功能影响 | 验证 |
|---|---|---|---|
| chromium-1228 / headless_shell-1228 | playwright 1.62.0 → 仅需 revision 1234（browsers.json） | 无 | build 后 run ✓ |
| patchright | `USE_PATCHRIGHT = False`（app/browser.py），import 有 try/except 保护 | 无（应急开关失效，可 pip install 恢复） | 构建成功 ✓ |
