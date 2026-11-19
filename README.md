# scholarly-pdf-parser · 科研 PDF 解析 Skill

面向「阅读01｜文献文档解析」：把科研 PDF 解析成**可核验的结构化结果**——按阅读顺序还原正文，识别章节层级、图表题注、
表格、公式、脚注与参考文献；每个结果都带 `页码 + bbox + 区块 id`，读不出的部分如实标注，而不是靠猜补齐。

> 这是开发阶段的说明。文中的数字全部来自我在 5 篇**公开 arXiv 论文**上的自测，不是平台成绩，也不是正式案例结果。

## 它解决什么（对应题面四个问题）
| 题面问题 | 做法 | 自查手段 |
|---|---|---|
| 提得全 | 正文/题注/脚注/参考文献全部保留；页眉页脚、旋转边注单独归类、不混入正文 | `check_parse.py` 与独立文本提取做逐页词召回对比 |
| 排得对 | 栏沟检测 + 递归 XY-cut；跨栏/跨页段落自动续接并记录 | 顺序倒置检测；合成「内容流乱序」压力测试 |
| 拆得清 | 章节编号/字号分级、表格行列（含无竖线表格重建、跨页表合并）、公式编号、上下标标记 | 表格行列一致性检查、标题编号断档检查 |
| 核得回 | 每个块 `id/page/bbox`；`locate_evidence.py` 把引文定位回页码，找不到就返回 `found:false` | `build_result.py --validate` 校验 result.json |

## 目录
```
SKILL.md                      Agent 入口：何时用、步骤、异常处理、诚实规则
README.md                     本文件（人类使用说明）
scripts/parse_pdf.py          解析主程序（parsed.json / parsed.md / quality_report.json）
scripts/check_parse.py        解析后自检（与独立提取对比，给出 pass/warn/fail 与建议）
scripts/locate_evidence.py    引文 → 页码/区块/bbox 定位
scripts/build_result.py       按 /input/case.json 的 output_contract 生成并校验 result.json
references/output-schema.md   parsed.json 字段说明
references/failure-modes.md   故障现象 → 原因 → 处置表，含无 Python 库时的 shell 兜底
references/method-notes.md    方法细节、自测数据、已知限制
examples/minimal-example.md   最小调用示例（真实运行输出，已标注为示例）
examples/case.example.json    虚构的契约示例（仅演示格式）
GITHUB_URL.txt                作品的 GitHub 公开链接
```

## 依赖与安装
- Python 3.8+；首选 `pymupdf`（开发时用 2.1 / MuPDF 1.28；需要 ≥1.24 才有 `find_tables`、`cluster_drawings`）。
- 自动回退：`pdfplumber`（测过 0.11）→ `pypdf`（降级：无几何信息，结果状态为 `partial`）。
- 可选 OCR：`tesseract` 命令行（及语言包），或 `pytesseract`。
- 安装：`pip install pymupdf pdfplumber pypdf`；运行时不需要联网。
- 来源与版本：均为 PyPI 官方包；脚本仅使用标准库 + 上述库。

## 使用步骤
```bash
python3 scripts/parse_pdf.py paper.pdf --out parsed            # 1 解析
python3 scripts/check_parse.py paper.pdf parsed --out check.json   # 2 自检
python3 scripts/locate_evidence.py parsed --query "引用的原文句子"  # 3 取证（按需）
python3 scripts/build_result.py --case /input/case.json --parsed parsed \
        --check check.json --answers answers.json --out result.json   # 4 生成并
python3 scripts/build_result.py --case /input/case.json --validate result.json   #   校验
```
`parse_pdf.py` 常用参数：`--ocr auto|off|force`、`--lang eng|chi_sim|eng+chi_sim`、
`--backend auto|pymupdf|pdfplumber|pypdf`、`--max-pages N`、`--no-tables`、`--ocr-budget 秒`。

## 输出
- `parsed.json`：全部结构（块、章节、表格、公式、参考文献、图、页眉页脚、`unparsed`、`warnings`）。
- `parsed.md`：可读版本，块前有 `<!-- id pN -->` 锚点；表格为 Markdown，含合并单元格时给 HTML。
- `quality_report.json`、`check.json`：状态、警告、逐页召回、问题清单。
- `result.json`：含 `case_id`、契约全部字段、`status`、`sources`、`uncertainty`、`change_records`；
  无法确定的字段为 `null` 并列入 `_meta.unfilled_fields`，不会被编造。

## 异常与处置（详见 references/failure-modes.md）
加密 PDF → 拒绝并提示向用户索取密码；非 PDF/损坏 → `failed` + 原始报错；扫描页 → OCR（有 tesseract 时），否则标
`needs_ocr`；乱码字体 → 标 `garbled_text_layer` 并建议 OCR；`þ/¼` 一类字体错码 → 仅在文档内有足够证据时修复并记录；
无竖线表格 → 按词位置重建并标注中等置信度。

## 自测结果（开发样本，非平台成绩）
| 论文 | 页数 | 版式 | 耗时 | 段落匹配 | 顺序正确 | 词召回 |
|---|---|---|---|---|---|---|
| ResNet 1512.03385 | 12 | CVPR 双栏 | ~5 s | 96.1 % | 100 % | 99.7 % |
| BERT 1810.04805 | 16 | ACL 双栏 + 无竖线表格 | ~5 s | 92.9 % | 99.0 % | 98.7 % |
| Attention 1706.03762 | 15 | 单栏 + 公式 | ~4 s | 98.4 % | 100 % | 99.8 % |
| GPT-3 2005.14165 | 75 | 单栏、40 张表 | ~20 s | 97.5 % | 100 % | 99.9 % |
| LIGO 1602.03837 | 16 | PRL 双栏 + 公式 | ~5 s | — | — | 99.8 % |

- 标准答案：ar5iv（由 LaTeX 源码生成的 HTML）的段落顺序；图表因浮动位置不同而排除；GT 不含公式，故部分段落无法匹配。
- 对照朴素基线（PyMuPDF `get_text("blocks")`）：在这些 LaTeX 生成的 PDF 上，默认块顺序本身已接近阅读顺序
  （基线 96.6–100 %），所以**顺序上并无优势**；常用的 `sort=True` 在双栏页反而降到 85 % 左右。
  本 Skill 的价值在结构、表格保真（如小数点不丢）、证据定位与自检。
- **合成压力测试**（BERT 前 8 页：版式不变、内容流顺序打乱，类似 Word/InDesign 导出）：
  本 Skill 顺序正确 98.2 %，基线默认 60.4 %、`sort=True` 83.0 %。

## 已知限制
- OCR 只用**桩程序**验证了接口与坐标换算，本机没有真实 OCR 引擎，OCR 精度**未验证**。
- 未在真实乱码字体、中日韩 PDF、旋转页面、三栏版式上做过测试（代码路径存在）。
- 公式为文本层线性化结果，非 LaTeX；堆叠的上下标（如 `+0.03 / −0.04`）可能被拆开。
- 无竖线表格为启发式重建；数字列间距极窄时可能合并。
- 并排表格可能共用一个题注。

## 版本与维护
`tool_version 1.0.0` · schema `scholarly-pdf-parser/1`。问题与改进请在 GitHub 仓库提 issue。
