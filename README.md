# 本地发票整理

<p align="center">
  <img src="icon.png" alt="本地发票整理" width="128">
</p>

<p align="center"><strong>北师香港浸会大学 Navigator Lab 开源项目</strong></p>

<p align="center">Local Invoice Organizer · Navigator Lab</p>

一个在本机运行的发票整理工具，用于上传发票和购买凭证、识别金额、核验、人工复核、匹配、重命名和 ZIP 归档。

项目不依赖飞书审批，也不会自动匹配历史审批。默认文件、数据库和归档包均保存在本机。

项目仓库：[MXQUKI/local-invoice-organizer](https://github.com/MXQUKI/local-invoice-organizer)。问题反馈请使用 [Issues](https://github.com/MXQUKI/local-invoice-organizer/issues)。

> 当前上传页面只提供“发票”和“购买凭证”两个类别。历史批次中的其他类别文件仍可兼容读取和归档，但不能从新上传页面选择。

> “本地整理”不等于所有识别都离线：发票 PDF 文字提取、重命名和归档在本机完成；启用 DeepSeek 后，购买凭证图片会发送到 DeepSeek API。公开源码不包含 API Key。

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
- 页面右上角一键退出，停止后台服务并释放端口，已保存的数据保留。

## 业务规则

购买凭证阈值按“单张发票”判断，默认 150 元：

| 单张发票金额 | 购买凭证 |
| ---: | :--- |
| 小于 150 元 | 不需要 |
| 等于 150 元 | 不需要 |
| 大于 150 元 | 必须提供 |

多张发票合计超过阈值不会触发要求；只有单张发票金额严格大于阈值才需要购买凭证。默认金额误差为 0.01 元，可在创建批次时修改。一张 290 元发票可以对应 100 元和 190 元两份凭证；匹配依据是识别或人工确认的金额，不是文件名。自动匹配最多尝试 5 份凭证的金额组合；存在多种可行组合时应人工确认。

人工拖拽匹配表示上传人已明确确认凭证归属。拖拽成功后，凭证默认标记为 `PASS`，购买凭证的实际付款金额默认补为对应发票金额，并将对应发票从自动金额差额阻断中排除；原 OCR 金额仍保留用于审计，汇总中会记录“人工确认覆盖发票金额”。

金额汇总会分开显示：需购买凭证发票总额、无需购买凭证发票金额，以及仅针对需购买凭证发票计算的凭证核对差额。低于或等于阈值的发票不会参与凭证核对差额计算。

## 环境要求

以下 Python 依赖适用于源码运行和构建；使用自包含 DMG/EXE 无需另装 Python。

- Python 3.10 或更高版本。
- macOS、Windows 或其他可运行 Python 的系统。
- Chrome、Safari、Edge 等现代浏览器。
- 建议安装 Tesseract；macOS 构建包还可以使用 macOS Vision OCR。
- DeepSeek Vision 为可选能力，需要 API Key 和网络连接。

发票 PDF 文字提取建议安装 `pypdf>=6.0`，以使用和安装包一致的文字排序与关键词检索流程；其他已有文字提取工具作为兼容后备。构建 DMG/EXE 需要安装 requirements-build.txt。

## 快速开始

### 使用已构建的安装包

- macOS：打开 DMG，将应用拖入“应用程序”，再双击启动。安装包架构需与电脑匹配；Apple 芯片版本不能用于 Intel Mac。
- Windows：运行 EXE，程序会启动本地服务并打开浏览器。
- 程序使用浏览器作为界面，不必一直保持 DMG 挂载。退出时使用页面右上角“退出程序”，不要仅关闭标签页。
- DeepSeek 需要联网且 API 配置有效；无内置 Key 的公开构建需要使用者自行配置。发票 PDF 内嵌文字提取不需要 API Key。

### 源码运行：macOS/Linux

~~~bash
git clone https://github.com/MXQUKI/local-invoice-organizer.git
cd local-invoice-organizer
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install "pypdf>=6.0"
python3 app.py --open-browser
~~~

### 源码运行：Windows PowerShell

~~~powershell
git clone https://github.com/MXQUKI/local-invoice-organizer.git
cd local-invoice-organizer
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install "pypdf>=6.0"
py app.py --open-browser
~~~

打开 http://127.0.0.1:8765。点击页面右上角“退出程序”并确认，可停止后台服务、关闭数据库连接并释放端口；页面提示“程序已退出”后可关闭浏览器标签。仅关闭网页不会停止后台程序。终端启动时也可按 Ctrl+C 停止。

上传、识别、保存或归档期间暂不能退出，请等待任务完成。退出会保留已保存的批次、原始文件和归档包；尚未保存的页面修改不会保留。更新安装包前请先退出旧程序，再启动新版，避免仍连接到旧版后台。

浏览器可能不允许网页自动关闭标签，因此“程序已退出”后仍显示提示页面是正常现象，不代表后台仍在运行。

检查环境但不启动服务：

~~~bash
python3 app.py --check
~~~

指定端口：

~~~bash
python3 app.py --port 8766 --open-browser
~~~

程序默认只监听 127.0.0.1。除非明确了解风险，否则不要使用 --host 0.0.0.0。

### 更新已有安装

1. 保存正在编辑的内容，等待上传、识别或归档任务完成。
2. 点击旧版页面的“退出程序”。若旧版尚无该按钮，终端启动的进程可按 Ctrl+C，macOS 打包程序可在“活动监视器”中按名称查找并退出。
3. 替换 app/EXE，或更新源码和依赖后重新启动；刷新浏览器页面。
4. 需要应用新版 PDF 解析逻辑时，新建批次并重新上传原文件。旧批次字段不会自动被覆盖，“重新核验”也不会重新提取文字；同一批次重复上传会被去重。

启动时若默认端口已有服务，程序可能直接打开现有网页。因此即使双击了新版安装包，后台仍可能是未退出的旧版。

## 使用流程

1. 新建批次，填写批次名称、购买方名称、购买方税号、允许年份、购买凭证阈值和金额误差。
2. 在上传区域选择“发票”或“购买凭证”。
3. 批量上传 PDF、PNG、JPG/JPEG、CSV、XLSX 或 TXT 文件。
4. 查看识别结果和 DeepSeek 处理进度。
5. 在“已匹配池”和“未匹配池”中确认关系，可多选拖拽凭证。两个匹配池只显示单张金额超过当前批次购买凭证阈值的发票；低于或等于阈值的发票仍可在下方文件表格查看、审核和归档，金额待补录的发票需先在表格中填写金额。
6. 人工补录缺失字段，或点击文件名预览原始文件。
7. 点击“重新核验”，处理 NEEDS_REVIEW、MULTI_ORDER_REVIEW 和 INCOMPLETE。
8. 核验通过后点击“生成 ZIP”。

文件表格中的“删除”按钮可以删除单个发票或购买凭证。删除发票时，关联凭证会保留但移回未匹配池；删除凭证时，只删除该凭证。删除任意文件都会使旧归档包失效，需要重新核验并生成 ZIP。

同一批次内发票号码重复时，发票卡片和文件表格会显示重复号码及其他重复文件的原文件名；展开“重复发票”可点击文件名预览。同名文件会附上文件 ID 前八位以便区分。修改号码或删除重复文件后会重新核验，自动清除已解除的重复提示；旧批次点击“重新核验”也能补全具体文件名。完全相同的文件仍按文件哈希去重，提示中会说明保留的是哪份文件。

重新加载批次数据时，页面会保留工作区、匹配区、文件表格和错误详情的滚动位置。

批次状态会按当前文件重新计算：空批次保持 `PROCESSING`；存在缺材料时为 `INCOMPLETE`；存在待审核或金额差异时为 `NEEDS_REVIEW`；没有阻塞问题但包含拒收文件时为 `REJECTED`；至少有一张有效发票且没有阻塞或拒收问题时为 `PASS`。`REJECTED` 批次可将拒收文件及原因一并归档，不代表这些发票核验通过。生成归档包后为 `ARCHIVED`，再次上传、编辑、匹配或删除文件会自动使旧归档包失效并重新核验。

## 识别流程

### 发票 PDF

~~~text
读取 PDF 内嵌文字
    ├── 有文字层：按文字坐标恢复行列，再按关键词检索字段
    └── 无文字层：转换页面图片，再调用 OCR
~~~

PDF 有文字层时，程序直接读取内嵌文字，依据文字坐标恢复标签与内容的对应关系，再检索“发票号码”“开票日期”“名称”“统一社会信用代码/纳税人识别号”和“价税合计（小写）”。购买方和销售方分开解析，不将整份文档去掉换行后拼接搜索。无法确定字段或出现冲突时保留为空并进入人工审核，不会用文件名或视觉模型猜测。有文字层但字段不完整时，不会重复 OCR。

打包依赖包含 `pypdf`，安装包与源码使用相同的文字提取路径。已有批次保存的是上传当时的结果，“重新核验”只校验已保存字段；更新解析器后，可新建批次重新上传原文件以重新识别。在原批次直接重复上传会触发文件去重，不会更新识别结果。

### 购买凭证图片

启用 DeepSeek 时，图片发送到官方当前推荐的 `deepseek-flash` 模型，只请求最终实付金额和判断依据。优惠金额、商品单价、订单金额和“共减”金额不直接作为最终付款金额。

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

默认 Base URL 为 `https://api.deepseek.com`，模型为 `deepseek-flash`。可用 `DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 和 `DEEPSEEK_TIMEOUT` 覆盖。程序使用 `/chat/completions` 接口，以原始图片字节编码为 Base64 `data:image/...` 的 `image_url` 发送，并使用 `detail: high` 保留原图。批量上传时，系统会并发处理最多 100 个文件，并在全部上传完成后统一执行一次匹配和核验，避免重复扫描批次。

根据 [DeepSeek 快速开始](https://api-docs.deepseek.com/zh-cn/) 和 [图像理解文档](https://api-docs.deepseek.com/zh-cn/guides/vision)，旧模型名 `deepseek-v4-flash` 和 `deepseek-v4-flash-vision-exp` 虽仍可调用，但对应模型已下线，请求会由最新 Flash 模型承接。新配置应使用 `deepseek-flash`。

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

脚本先做 API 健康检查，再按 `--workers` 指定的并发数处理 `sample/凭证`；默认 100，上述示例为 20。它不会写入主程序数据库、自动匹配或自动通过凭证。也可以根据 DeepSeek 账户的限流策略降低 `--workers`。这些测试会实际调用 API，可能产生费用。

`sample/` 已加入 `.gitignore`，克隆仓库不会包含真实发票和截图。运行上述命令前，请自行准备脱敏样例放入 `sample/发票` 和 `sample/凭证`，或将命令中的图片路径替换为自己的测试文件。

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
| PROCESSING | 正在识别、刚发生文件变更，或空批次等待上传 |
| PASS | 核验通过，可以归档 |
| ARCHIVED | 已生成归档包 |
| REJECTED | 文件被拒收 |
| NEEDS_REVIEW | 需要人工确认或补录 |
| MULTI_ORDER_REVIEW | 一张发票关联多个订单 |
| INCOMPLETE | 缺少高额发票所需购买凭证或其他材料 |

## 运行时配置

默认数据目录如下，备份时应复制整个目录，而不只是数据库：

| 运行方式 | 默认数据目录 |
| --- | --- |
| 源码运行 | 项目目录下的 `data/` |
| macOS 打包应用 | `~/Library/Application Support/本地发票整理/` |
| Windows 打包应用 | `%LOCALAPPDATA%\本地发票整理\` |

目录包含 `invoice.db`、`batches/` 和 `archives/`。`LOCAL_INVOICE_DATA_DIR` 优先于默认路径；源码版和打包版默认不会共用同一数据目录。建议完全退出程序后再备份或迁移。

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

必须先安装构建依赖，以生成包含 Python 运行时和 PDF 解析库的自包含 app。公开发布建议构建不含 Key 的版本：

~~~bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-build.txt
unset DEEPSEEK_API_KEY EMBEDDED_CONFIG_SOURCE
./build_macos.sh
~~~

输出：`dist/本地发票整理.dmg` 和 `dist/本地发票整理.app`。构建架构取决于 Python 环境，并非自动生成 Intel / Apple 芯片通用版。默认构建会重新创建 `build/` 和 `dist/`；构建前先退出旧 app，并将需要保留的旧安装包备份到这两个目录之外。

内部使用且明确接受密钥可被提取的风险时，才设置 `DEEPSEEK_API_KEY` 构建内置 Key 的版本。确认工作区没有遗留的 `embedded_config.py`，再构建用于公开分发的包。

#### 更换 API Key 后重新编译

更换 DeepSeek API Key 后，需要重新生成整个 DMG。不要直接修改已有 DMG 或 App 内部文件：

~~~bash
source .venv/bin/activate
DEEPSEEK_API_KEY="新的 API Key" ./build_macos.sh
~~~

构建脚本会把 Key 临时写入 `embedded_config.py`，编译完成后自动删除工作区中的临时文件。新 Key 会进入新的自包含 App，旧 DMG 不会自动更新。

模型和 API 地址是**运行时配置**，当前构建脚本只嵌入 Key，不会固化构建终端中的 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL`。临时切换可在启动时指定：

~~~bash
DEEPSEEK_MODEL="deepseek-flash" \
DEEPSEEK_BASE_URL="https://api.deepseek.com" \
"dist/本地发票整理.app/Contents/MacOS/本地发票整理"
~~~

若要让接收方双击时也使用新默认值，应修改 `ocr_image.py` 中相应默认值后重新构建，并同步更新配置示例和文档。

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
set DEEPSEEK_API_KEY=
build_windows.bat
~~~

PowerShell：

~~~powershell
py -m pip install -r requirements-build.txt
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
.\build_windows.ps1
~~~

输出：dist\\本地发票整理.exe。

上述命令用于不含 Key 的公开构建；同样需确保没有遗留的 `embedded_config.py`。内部构建可在执行脚本前设置 `DEEPSEEK_API_KEY`，但不要将生成的带 Key 安装包公开上传。

### GitHub Actions

仓库提供 [build-packages 工作流](https://github.com/MXQUKI/local-invoice-organizer/actions/workflows/build.yml)，支持手动运行或推送 `v*` 标签触发。工作流分别在 Windows 和 macOS 上构建，成功后可从对应运行的 Artifacts 下载。普通 `main` 分支推送不会触发此工作流，也不会自动创建 GitHub Release。当前工作流没有注入 API Key。

### API Key 安全

构建脚本可以临时生成 embedded_config.py，构建后删除工作区中的临时文件，但编译进 DMG/EXE 的 Key 仍可能被提取，不能视为安全存储。不要把真实 Key 提交到 Git、README、Issue、截图或日志，也不要公开上传带真实 Key 的安装包。面向不完全信任的用户时，建议使用服务端代理、额度和速率限制；开源发布建议不内置 Key，让使用者在本机环境变量中配置。

## 项目目录

~~~text
app.py                       本地服务、网页、核验、匹配和归档
invoice_text.py              PDF 内嵌文字排序与发票关键词检索
ocr_image.py                 图片、扫描 PDF 和 DeepSeek Vision 识别
evaluate_deepseek_samples.py DeepSeek 样例批量测试
macos_vision_ocr.swift       macOS Vision OCR 源码
desktop_entry.py             PyInstaller 启动入口
build_macos.sh               macOS DMG 构建脚本
build_windows.bat            Windows EXE 构建脚本
build_windows.ps1             Windows PowerShell 构建脚本
packaging/                   打包辅助脚本
requirements-build.txt       打包依赖
tests/                       PDF 解析、重复发票、并发上传和退出流程测试
icon.png                     应用图标
sample/                      自备的本地脱敏样例（不随仓库分发）
data/                        源码运行时的数据库、上传文件和归档包
dist/                        构建输出
~~~

data/、dist/、build/、API 测试结果和临时构建配置默认被 .gitignore 忽略。提交前检查：

~~~bash
git status --short
git diff --cached
~~~

## 开发与检查

~~~bash
python3 -m pip install "pypdf>=6.0"
python3 -m py_compile app.py invoice_text.py ocr_image.py desktop_entry.py
python3 app.py --check
python3 -m unittest discover -s tests -v
~~~

Windows 可将 `python3` 替换为 `py`。当前测试覆盖文字层优先、关键词边界、重复发票提示、205 个模拟上传任务的 100 并发上限，以及退出后进程终止、端口释放和重启后数据保留。测试使用临时目录，不修改真实批次，也不调用付费 API；JavaScript 交互测试需要 Node.js，缺少 Node.js 时相应测试会跳过。

网页批量上传和服务端识别并发上限当前为 100；样例脚本也默认使用 100 个并发，可通过 `--workers` 调整脚本并发。实际同时请求数还受浏览器连接数和 API 限流影响，100 是任务调度上限。若 API 返回 429，样例脚本可降低 `--workers`；主程序目前没有页面并发设置。

## 故障排查

- 页面打不开：确认终端出现“本地发票整理已启动：http://127.0.0.1:8765”；端口被占用时使用 --port 8766。
- 更新后仍是旧界面、没有退出按钮：关闭浏览器不等于退出后台。先停止旧 app/EXE，替换安装包并启动新版，再刷新网页。
- 发票仍显示旧的错误字段：先确认运行的是新版，再新建批次重新上传。重新核验只检查保存的字段，不会重新解析 PDF。
- 退出按钮暂不可用：当前页面仍有上传、识别或保存任务；其他页面有写入任务时，后台也会拒绝退出。请等任务完成后重试。
- 低于或等于阈值的发票在匹配池中找不到：这是预期行为，请在下方文件表格查看、审核，金额仍计入汇总和归档。
- 凭证没有自动匹配：检查识别金额、误差和候选组合，存在歧义时可多选凭证并拖到目标发票人工确认，不依赖文件名。
- 缺少中文语言包：运行 tesseract --list-langs，确认包含 chi_sim。
- DeepSeek 返回空金额：这是预期的安全行为，程序会标记 NEEDS_REVIEW，请人工填写或拖拽匹配。
- 无法生成 ZIP：点击“重新核验”，查看“当前不能归档”面板，处理高额发票缺少购买凭证、关键字段缺失、金额无法确认或金额差异等问题。

## 隐私与贡献

- 默认只监听 127.0.0.1，文件和数据库按“运行时配置”一节所列目录保存在本机。
- 启用 DeepSeek 时会上传原始购买凭证图片用于识别，不会在本地先压缩；使用前请确认自己有权向该服务提供这些材料。不含 Key 且未配置 Key 时，购买凭证不会发送到 DeepSeek。
- API Key 不应写入数据库、归档包或版本库。
- 欢迎提交 Issue 和 Pull Request，但不要提交真实发票、付款截图、API Key 或运行时 data/。

## 许可证

本项目采用 MIT License，详见根目录的 [LICENSE](LICENSE) 文件。
