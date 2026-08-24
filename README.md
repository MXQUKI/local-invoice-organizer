# 本地发票整理

<p align="center">
  <img src="icon.png" alt="本地发票整理" width="128">
</p>

<p align="center"><strong>北师香港浸会大学 Navigator Lab 开源项目</strong></p>

<p align="center">Local Invoice Organizer · Navigator Lab</p>

一个在本机运行的发票整理工具，用于上传发票和购买凭证、识别金额、核验、人工复核、匹配、重命名和 ZIP 归档。

项目不依赖飞书审批，也不会自动匹配历史审批。默认文件、数据库和归档包均保存在本机。

> 当前上传页面只提供“发票”和“购买凭证”两个类别。历史批次中的其他类别文件仍可兼容读取和归档，但不能从新上传页面选择。

## 功能

- 发票 PDF 优先读取内嵌文字；只有没有文字层时才进入图片 OCR。
- 解析发票金额、发票号码、购买方名称、购买方税号和开票日期。
- 校验购买方抬头、税号和开票年份。
- 购买凭证图片可选使用 DeepSeek Vision 识别最终真实付款金额。
- DeepSeek 不确定、空金额或请求失败时进入人工复核，不使用文件名猜金额。
- 按金额自动匹配凭证，支持多份凭证合计对应一张发票。
- 支持多选拖拽、移出匹配、人工补录金额和单击预览。
- 同一批次按 SHA-256 去重，重复文件直接删除本次副本。
- 错误信息折叠显示，识别过程显示进度，刷新后保留滚动位置。
- 自动重命名并生成金额汇总、XLSX、CSV 和 ZIP 归档包。

## 业务规则

购买凭证阈值按“单张发票”判断，默认 150 元：

| 单张发票金额 | 购买凭证 |
| ---: | :--- |
| 小于 150 元 | 不需要 |
| 等于 150 元 | 不需要 |
| 大于 150 元 | 必须提供 |

多张发票合计超过阈值不会触发要求；只有单张发票金额严格大于阈值才需要购买凭证。默认金额误差为 0.01 元，可在创建批次时修改。一张发票可匹配多份凭证，例如 290-1 + 290-2 = 290。

人工拖拽匹配表示上传人已明确确认凭证归属。拖拽成功后，凭证默认标记为 `PASS`，购买凭证的实际付款金额默认补为对应发票金额，并将对应发票从自动金额差额阻断中排除；原 OCR 金额仍保留用于审计，汇总中会记录“人工确认覆盖发票金额”。

金额汇总会分开显示：需购买凭证发票总额、无需购买凭证发票金额，以及仅针对需购买凭证发票计算的凭证核对差额。低于或等于阈值的发票不会参与凭证核对差额计算。

## 环境要求

- Python 3.10 或更高版本。
- macOS、Windows 或其他可运行 Python 的系统。
- Chrome、Safari、Edge 等现代浏览器。
- 建议安装 Tesseract；macOS 构建包还可以使用 macOS Vision OCR。
- DeepSeek Vision 为可选能力，需要 API Key 和网络连接。

主程序尽量使用 Python 标准库，运行时不要求安装完整依赖包；构建 DMG/EXE 需要安装 requirements-build.txt。

## 快速开始

### macOS/Linux

~~~bash
git clone <你的仓库地址>
cd 本地发票整理
python3 -m venv .venv
source .venv/bin/activate
python3 app.py --open-browser
~~~

### Windows PowerShell

~~~powershell
git clone <你的仓库地址>
cd 本地发票整理
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py app.py --open-browser
~~~

打开 http://127.0.0.1:8765，停止服务按 Ctrl+C。

检查环境但不启动服务：

~~~bash
python3 app.py --check
~~~

指定端口：

~~~bash
python3 app.py --port 8766 --open-browser
~~~

程序默认只监听 127.0.0.1。除非明确了解风险，否则不要使用 --host 0.0.0.0。

## 使用流程

1. 新建批次，填写批次名称、购买方名称、购买方税号、允许年份、购买凭证阈值和金额误差。
2. 在上传区域选择“发票”或“购买凭证”。
3. 批量上传 PDF、PNG、JPG/JPEG、CSV、XLSX 或 TXT 文件。
4. 查看识别结果和 DeepSeek 处理进度。
5. 在“已匹配池”和“未匹配池”中确认关系，可多选拖拽凭证。
6. 人工补录缺失字段，或点击文件名预览原始文件。
7. 点击“重新核验”，处理 NEEDS_REVIEW、MULTI_ORDER_REVIEW 和 INCOMPLETE。
8. 核验通过后点击“生成 ZIP”。

文件表格中的“删除”按钮可以删除单个发票或购买凭证。删除发票时，关联凭证会保留但移回未匹配池；删除凭证时，只删除该凭证。删除任意文件都会使旧归档包失效，需要重新核验并生成 ZIP。

重新加载批次数据时，页面会保留工作区、匹配区、文件表格和错误详情的滚动位置。

## 识别流程

### 发票 PDF

~~~text
读取 PDF 内嵌文字
    ├── 有文字层：直接解析发票字段，不调用 OCR
    └── 无文字层：转换页面图片，再调用 OCR
~~~

PDF 有文字层但字段不完整时，不会重复 OCR，缺失字段进入人工审核。

### 购买凭证图片

启用 DeepSeek 时，图片发送到 deepseek-v4-flash-vision-exp，只请求最终实付金额和判断依据。优惠金额、商品单价、订单金额和“共减”金额不直接作为最终付款金额。

模型返回 payment_amount 为 null、图片无法确认、API 失败或格式异常时进入人工复核。未配置 DeepSeek 时，程序可以使用本地 macOS Vision 或 Tesseract，但本地识别结果仍需人工核对。

## DeepSeek Vision（可选）

### 配置

macOS/Linux：

~~~bash
export DEEPSEEK_API_KEY="你的 DeepSeek API Key"
export DEEPSEEK_VISION_ENABLED=1
python3 app.py --open-browser
~~~

Windows PowerShell：

~~~powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
$env:DEEPSEEK_VISION_ENABLED = "1"
py app.py --open-browser
~~~

默认 Base URL 为 https://api.deepseek.com，模型为 deepseek-v4-flash-vision-exp。可用 DEEPSEEK_BASE_URL、DEEPSEEK_MODEL 和 DEEPSEEK_TIMEOUT 覆盖。程序使用 /chat/completions 接口，以原始图片字节编码为 Base64 data:image/... 的 image_url 发送，不会压缩或修改凭证图片。批量上传时，系统会并发处理最多 20 个文件，并在全部上传完成后统一执行一次匹配和核验，避免重复扫描批次。

测试 API：

~~~bash
DEEPSEEK_API_KEY="你的 API Key" \
python3 ocr_image.py sample/凭证/2182.PNG --backend deepseek --json
~~~

批量测试样例：

~~~bash
export DEEPSEEK_API_KEY="你的 API Key"
python3 evaluate_deepseek_samples.py --workers 20 \
  --json-output deepseek_receipts_review.json \
  --csv-output deepseek_receipts_review.csv
~~~

脚本先做 API 健康检查，再以最多 100 个并发处理 sample/凭证；不会写入主程序数据库、自动匹配或自动通过凭证。也可以根据 DeepSeek 账户的限流策略降低 `--workers`。

`sample/` 只适合放脱敏测试文件。当前工作区中的样例目录已加入 `.gitignore`，公开仓库应使用不含个人信息的测试样例替换，或在文档中提供下载方式。

## OCR 配置

macOS 安装 Tesseract 和中文语言包：

~~~bash
brew install tesseract
brew install tesseract-lang
tesseract --list-langs
~~~

输出中应包含 chi_sim。只识别英文和数字时：

~~~bash
python3 ocr_image.py image.jpg --lang eng --json
~~~

自定义路径：

~~~bash
export TESSERACT_CMD=/path/to/tesseract
export VISION_OCR_CMD=/path/to/macos_vision_ocr
export PDFTOTEXT_CMD=/path/to/pdftotext
~~~

如果系统没有可用的 PDF 文字提取工具，可以在虚拟环境中安装可选库：

~~~bash
python3 -m pip install pypdf pdfplumber
~~~

macOS Vision 源码位于 macos_vision_ocr.swift；构建 DMG 时，build_macos.sh 会在可用时自动编译。

## 归档输出

~~~text
发票整理_批次名称_批次编号.zip
├── 00_金额汇总.txt
├── 00_核销汇总.xlsx
├── 01_发票/
├── 02_购买凭证/
├── 99_拒收/
└── 汇总.csv
~~~

历史批次中的付款凭证、退款凭证和其他材料会按兼容目录归档。有效发票通常重命名为 发票金额_发票号码后四位.扩展名，例如 2148.00_7761.pdf。汇总表保留原文件名，并记录识别方式、金额、购买方信息、处理状态和异常说明。

## 状态

| 状态 | 含义 |
| --- | --- |
| PROCESSING | 正在识别或处理 |
| PASS | 核验通过，可以归档 |
| ARCHIVED | 已生成归档包 |
| REJECTED | 文件被拒收 |
| NEEDS_REVIEW | 需要人工确认或补录 |
| MULTI_ORDER_REVIEW | 一张发票关联多个订单 |
| INCOMPLETE | 缺少高额发票所需购买凭证或其他材料 |

## 运行时配置

| 环境变量 | 作用 |
| --- | --- |
| LOCAL_INVOICE_DATA_DIR | 自定义数据库、上传文件和归档目录 |
| TESSERACT_CMD | Tesseract 路径 |
| VISION_OCR_CMD | Vision OCR 路径 |
| PDFTOTEXT_CMD | pdftotext 路径 |
| DEEPSEEK_API_KEY | DeepSeek API Key |
| DEEPSEEK_VISION_ENABLED | 启用或关闭 DeepSeek Vision |
| DEEPSEEK_BASE_URL | API 基础地址 |
| DEEPSEEK_MODEL | 视觉模型名称 |
| DEEPSEEK_TIMEOUT | 请求超时时间 |

示例：将数据放在项目目录之外：

~~~bash
export LOCAL_INVOICE_DATA_DIR="$HOME/.local/share/local-invoice-organizer"
python3 app.py --open-browser
~~~

## 打包发布

### macOS DMG

必须先安装 PyInstaller，以生成包含 Python 运行时的自包含 app：

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-build.txt
DEEPSEEK_API_KEY="你的 API Key" ./build_macos.sh
~~~

输出：dist/本地发票整理.dmg。

#### 更换 API Key 后重新编译

更换 DeepSeek API Key 后，需要重新生成整个 DMG。不要直接修改已有 DMG 或 App 内部文件：

~~~bash
source .venv/bin/activate
DEEPSEEK_API_KEY="新的 API Key" ./build_macos.sh
~~~

构建脚本会把 Key 临时写入 `embedded_config.py`，编译完成后自动删除工作区中的临时文件。新 Key 会进入新的自包含 App，旧 DMG 不会自动更新。

如果要更换模型或 API 地址：

~~~bash
DEEPSEEK_API_KEY="新的 API Key" \
DEEPSEEK_MODEL="deepseek-v4-flash-vision-exp" \
DEEPSEEK_BASE_URL="https://api.deepseek.com" \
./build_macos.sh
~~~

如果当前终端没有设置环境变量，但需要复用一个已经生成的嵌入配置文件，可以显式指定：

~~~bash
EMBEDDED_CONFIG_SOURCE="/path/to/embedded_config.py" ./build_macos.sh
~~~

不要把 `embedded_config.py`、真实 API Key 或带真实 Key 的 DMG 提交到 Git 或公开发布。

构建脚本会执行应用内置启动测试并进行代码签名验证；测试失败时不会生成 DMG。没有 PyInstaller 时构建会直接停止，避免误生成依赖接收方 Python 环境的不可分发安装包。如仅需本机调试旧式包装器，可以显式设置 `ALLOW_PYTHON_WRAPPER=1`。

应用启动异常会写入 `~/Library/Logs/本地发票整理.log`，并在 macOS 上显示错误弹窗。重复双击时如果 8765 端口已有本程序运行，会直接打开现有页面。

默认使用 ad-hoc 签名，适合本机和内部测试。正式公开发布建议设置 `MACOS_CODESIGN_IDENTITY` 使用 Apple Developer ID，并通过 `MACOS_NOTARY_PROFILE` 启用 notarization；否则从互联网下载后仍可能出现“无法验证开发者”，用户需右键应用选择“打开”。

### Windows EXE

必须在 Windows 环境构建，不能用 macOS Python 交叉编译：

~~~bat
py -m pip install -r requirements-build.txt
set DEEPSEEK_API_KEY=你的 API Key
build_windows.bat
~~~

PowerShell：

~~~powershell
py -m pip install -r requirements-build.txt
$env:DEEPSEEK_API_KEY = "你的 API Key"
.\build_windows.ps1
~~~

输出：dist\\本地发票整理.exe。

### API Key 安全

构建脚本可以临时生成 embedded_config.py，构建后删除工作区中的临时文件，但编译进 DMG/EXE 的 Key 仍可能被提取，不能视为安全存储。不要把真实 Key 提交到 Git、README、Issue、截图或日志，也不要公开上传带真实 Key 的安装包。面向不完全信任的用户时，建议使用服务端代理、额度和速率限制；开源发布建议不内置 Key，让使用者在本机环境变量中配置。

## 项目目录

~~~text
app.py                       本地服务、网页、核验、匹配和归档
ocr_image.py                 图片、扫描 PDF 和 DeepSeek Vision 识别
evaluate_deepseek_samples.py DeepSeek 样例批量测试
macos_vision_ocr.swift       macOS Vision OCR 源码
desktop_entry.py             PyInstaller 启动入口
build_macos.sh               macOS DMG 构建脚本
build_windows.bat            Windows EXE 构建脚本
build_windows.ps1             Windows PowerShell 构建脚本
packaging/                   打包辅助脚本
requirements-build.txt       打包依赖
icon.png                     应用图标
sample/                      本地测试样例（发布前应替换为脱敏样例）
data/                        运行时数据库、上传文件和归档包
dist/                        构建输出
~~~

data/、dist/、build/、API 测试结果和临时构建配置默认被 .gitignore 忽略。提交前检查：

~~~bash
git status --short
git diff --cached
~~~

## 开发与检查

~~~bash
python3 -m py_compile app.py ocr_image.py evaluate_deepseek_samples.py
python3 app.py --check
python3 ocr_image.py sample/凭证/2182.PNG --json
~~~

网页批量上传和服务端识别并发上限当前为 100；测试脚本默认使用 100 个并发，也可通过 `--workers` 调整。若 API 返回 429 限流错误，建议降低并发。

## 故障排查

- 页面打不开：确认终端出现“本地发票整理已启动：http://127.0.0.1:8765”；端口被占用时使用 --port 8766。
- 缺少中文语言包：运行 tesseract --list-langs，确认包含 chi_sim。
- DeepSeek 返回空金额：这是预期的安全行为，程序会标记 NEEDS_REVIEW，请人工填写或拖拽匹配。
- 无法生成 ZIP：点击“重新核验”，查看“当前不能归档”面板，处理高额发票缺少购买凭证、关键字段缺失、金额无法确认或金额差异等问题。

## 隐私与贡献

- 默认只监听 127.0.0.1，文件和数据库默认保存在本机 data/。
- 未配置 DeepSeek 时，购买凭证不会发送到远程视觉 API；配置后请确认隐私政策和合规要求。
- API Key 不应写入数据库、归档包或版本库。
- 欢迎提交 Issue 和 Pull Request，但不要提交真实发票、付款截图、API Key 或运行时 data/。

## 许可证

本项目采用 MIT License，详见根目录的 [LICENSE](LICENSE) 文件。
