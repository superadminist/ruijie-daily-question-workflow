---
name: "ruijie-daily-question-workflow"
description: "管理锐捷每日题库脚本从日期目录 bak 原题、py 单文件修复、用户自定义外部目录日志接收、log 单文件回流、FAIL 迭代、PASS 终验、规格目录封包归档、exe check 文件汇集和 ignore 失败报表导出的完整闭环。适用于用户提到最新日志已经更新、日志已上传、执行机日志已回流、设置日志接收目录、同前缀最新日志、PASS/FAIL 回流、终验、封包、exe check、生成ignore表、生成ignore、ignore报表、导出ignore 或处理下一个 py 文件。"
---

# Ruijie Daily Question Workflow

## 路径配置

- **脚本生成器技能**: `../.trae/skills/ruijie-test-script-generator/SKILL.md`
- **红线问题文档**: `../zsk/红线问题.md`
- **日志接收目录**: `D:\MyCode\RJ-pytest\otherlog`

普通用户只需设置上面的“日志接收目录”，无需执行 Python 配置命令。用户在对话中提供目录时，先确认该目录存在，再仅修改这一行；保存绝对路径，例如 `D:\RuijieWorkSpace\Logs`。

本 skill 是上层工作流编排。具体脚本命令、断言、SRS 覆盖和日志失败分析必须继续调用「脚本生成器技能」；红线检查必须读取「红线问题文档」。

上级新增的《脚本编写注意事项》是脚本编写与审查的主导规范：处理所有继承 `class_setup_deardown` 的脚本时，应优先按其中的红线规则和通用检查清单执行。若旧参考示例、历史脚本写法或 generator 的编码参考片段与《脚本编写注意事项》的红线/检查清单冲突，以《脚本编写注意事项》为准。

## 目录约定

每日题库目录放在当前工程目录下，例如 `0512/`：

```text
0512/
  bak/   原始题目脚本，只保留原件，不直接修改
  py/    当前正在处理或等待执行机测试的脚本，每次只能有一个 .py
  log/   当前脚本最新日志工作台，接收完成后只能保留一个 .txt
  done/  已封包完成的终验结果，按 SRS 规格目录保存终版脚本和最新 PASS 日志
         规格目录下使用 Log/ 保存归档后的 PASS 日志
  check/ exe check 扁平文件汇集目录；根目录只保存 done/ 中的 .py 和 .txt，不创建子目录
  ignore/ 已确认跳过或退回的问题归档；工作流只在用户明确要求时写入
    test_xxx/
      test_xxx.py
      Log/
        test_xxx_结果(FAIL或PASS)_用时(xx).txt
      error.json
```

执行机日志接收目录不属于日期工作目录，由用户在本文件“路径配置”中设置为任意已存在目录。接收目录可包含多次回流或同名日志所在的不同子目录。旧版本保存在 `<date_dir>/.workflow_state.json` 的 `log_receive_dir` 仍作为兼容回退。

## 硬规则

- 每次只处理 `py/` 中的一个 `.py`；如果 `py/` 有多个脚本，先停止并要求整理。
- 日志必须按当前脚本文件名前缀匹配：`py/test_A.py` 只允许使用 `log/test_A*.txt`。
- 日志接收目录由用户任意设置，必须是已存在目录；默认读取本文件“日志接收目录”配置，不要求普通用户执行 Python 命令；不得擅自创建、清理或固定到日期工作目录中；接收阶段只移动选中的最新日志，不得删除外部目录中的其他文件。
- 收到“最新日志已经更新”“日志已更新”“日志已上传”“最新日志已上传”“执行机日志已回流”等同义指令时，运行 `receive-log`，从已配置的外部接收目录递归查找当前脚本同前缀 `.txt`，按最后修改时间取最新文件。
- `receive-log` 找到匹配文件后，将其移动到 `log/`，并清除 `log/` 中其他全部内容；成功后 `py/` 和 `log/` 必须分别只有一个当前 `.py` 和一个最新 `.txt`。
- 修改或修复 `py/` 中当前脚本后，禁止删除、移动或清空 `log/` 中现有日志；旧日志应保留用于问题追溯。只有成功执行 `receive-log` 接收到同前缀新日志时，才允许用新日志替换旧日志。
- 同前缀日志有多个或存在不同子目录中的同名日志时，只取最后修改时间最新的一个。
- 最新同前缀日志为 PASS 时，即使日志修改时间早于当前脚本，也允许继续终验和封包；`log_is_fresh` 只作为时间关系提示，不作为 PASS 封包门禁。
- PASS 后又修改 `.py` 时，旧 PASS 日志仍可用于封包；必须明确提示日志早于脚本，其他日志状态、前缀匹配、异常关键字和人工终验规则保持不变。
- `bak/` 中原始脚本不可修改；修正只发生在 `py/` 中的工作副本。
- 收到“exe check”指令时，先检查 `done/` 下全部 `.py`、`.txt` 是否存在同名文件；无重名时清空同级 `check/`，再把文件直接复制到 `check/` 根目录，禁止复制或创建任何规格子目录；有重名时停止且不得清理现有 `check/`。
- `ignore/` 中的脚本视为已处理且需要跳过，`advance` 推进时必须跳过；工作流禁止自行判断并移动文件到 `ignore/`，只有用户明确说明“无法解决，归档到 ignore”或“锐捷人工退回如下 JSON”时才能执行归档命令。
- `ignore/<脚本stem>/error.json` 必须使用结构：`{"pyName": "脚本名.py", "failReason": "原因"}`。执行机 FAIL 无法解决时，`failReason` 可先留空供用户手动填写；锐捷人工退回时，必须写入用户提供的原因。
- 收到“生成ignore表”“生成ignore”“ignore报表”“导出ignore”任一明确指令时，生成当前工作目录的 Ignore Excel；不得把“工作结束”等普通表达自动视为生成指令。
- Ignore Excel 数据只读取 `ignore/<脚本stem>/error.json`，并校验同目录存在唯一且与 `pyName` 一致的 `.py`。`failReason` 包含“回收”的记录进入“回收”，其余进入“卡点”；全部为卡点时只生成“卡点”，全部为回收时只生成“回收”，两类都有时按“卡点”“回收”顺序生成两个 Sheet。
- Ignore Excel 保存为 `<date_dir>/<日期目录名>_失败_<总数量>.xlsx`，例如 `0821/0821_失败_2.xlsx`。同名文件仅在新文件生成并验证成功后替换，不删除其他数量的历史报表，也不修改、移动或删除 `ignore/` 中任何内容。
- PASS 后最终封包前，删除 docstring 中 `@具体UI命令（脚本完成后删除）:` 行。
- 用例等级与日志运行时间绑定：L0/L1 要求 10 分钟以内，L2/L3 要求 30 分钟以内；若最新 PASS 日志文件名或内容显示用时超过 10 分钟，例如 `结果(PASS)_用时(17).txt`，封包时应把归档脚本的 `@用例等级` 提升到 L2，并在终验报告中核对脚本和日志中的 `@用例等级`。
- PASS 日志也必须检查真实业务步骤内的明显异常关键字，防止命令不存在却假 PASS。重点检查 `% Invalid input`、`% Unknown command.`、`% Unknowm command.`、`incomplete command`、`ambiguous command`、`Bad parameter`、`does not exist`、`can't find`、`Traceback`、`AttributeError`、`TypeError`。只检查脚本真实步骤对应的日志块，不检查框架健康检查、coredump、配置对比、内存显示等框架区域；如果真实步骤中命中异常关键字，禁止终验通过和封包。
- 脚本静态审查以《脚本编写注意事项》为主导：禁止 `exec()` 和任何含 `exec` 的方法名/变量名/字段名；禁止 `python.skip/pytest.skip`；禁止 `@pytest.mark.usefixtures(...)`；禁止除框架方法外的自定义函数；禁止从 `self.tb` 手工提取设备属性；禁止硬编码物理接口；禁止除 `setup_method` 外的 `try...except`；禁止以 `FAIL` 作为最终通过条件；非 `teardown_method` 的配置命令必须使用 `cmd_list` 并通过 `cmgr.check_write_cmd(*cmd_list)` 下发；`teardown_method` 的配置恢复使用 `cmd_list` + `cmgr.command(*cmd_list)`，不做校验和断言，由框架配置对比判断是否清空；包含配置/清理命令的 `cmd_list` 首条必须是 `enable` 或 `en`；业务回显验证应使用公共库判断函数后断言 `PASS`。

## 常用脚本

使用 `scripts/workflow.py` 做机械状态判断：

```bash
python3 ruijie-daily-question-workflow/scripts/workflow.py init 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py status 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py lint 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py log-summary 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py final-check 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py advance 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py package 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py exe-check 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py receive-log 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py ignore-current 0512
python3 ruijie-daily-question-workflow/scripts/workflow.py reject-done 0512 --items-json '[{"pyName":"test_xxx.py","failReason":"退回原因"}]'
```

`status` 会输出当前 py、同前缀最新日志、PASS/FAIL 状态、日志修改时间是否晚于脚本，并给出 `next_action` 短决策字段。`receive-log` 默认读取本文件“日志接收目录”，把最新匹配日志归一化到单文件 `log/`。旧的 `set-receive-dir`、`init --receive-dir` 和 `receive-log --receive-dir` 仅保留为兼容备用，不作为普通用户操作步骤。`package` 执行封包，只在同前缀最新日志为新鲜 PASS 时执行。`exe-check` 清空 `check/` 后，仅将 `done/` 下全部 `.py`、`.txt` 扁平复制到 `check/` 根目录。

为降低上下文占用，优先使用辅助命令生成短报告：

- `lint`：静态检查当前 `py/` 脚本的红线、SRS 字段、`step.expect` 有效断言、`cmd_list` 首条 `enable`、非 teardown 配置的 `check_write_cmd`、禁止自定义函数、禁止 `try...except`、禁止硬编码接口、拓扑一致性、`teardown_method` 步骤壳等问题。
  - `test_process` 内的 `step.expect` 必须含有效 `assert`。
  - `teardown_method` 必须使用 `casestep("配置清空") + expect("配置清除成功")` 步骤壳；配置恢复使用 `cmd_list` + `cmgr.command(*cmd_list)`，不做校验和断言，由框架配置对比判断是否清空；禁止为消除 `EXPECT_WITHOUT_ASSERT` 删除 teardown 步骤壳后只保留裸清理命令。
  - teardown 的 expect 无 assert 且无 `command/check_write_cmd` 实际清理动作时，报 `TEARDOWN_EXPECT_EMPTY`；缺步骤壳时报 `TEARDOWN_SHELL_MISSING`。
- `log-summary`：只摘要最新同前缀日志，输出日志状态、步骤线索、FAIL 错误原因和错误附近片段，避免整份日志进入上下文。
- `final-check`：终验前汇总状态、静态检查结果、docstring 步骤/预期、代码 step/expect、日志步骤线索、日志用时和用例等级建议，供模型做最终人工判断。

## 工作流

1. 初始化或检查日期目录：
   - 若用户给出日期目录，使用该目录；否则按当前日期推断，例如 `0512/`。
   - 若本文件“日志接收目录”为“未设置”，要求用户提供路径；确认目录存在后，只将该配置行修改为绝对路径。
   - 运行 `workflow.py init <date_dir>`，确保 `bak/py/log/done/check/ignore` 存在。初始化不创建外部接收目录。
   - 如果日期目录下存在 `ignore/`，先说明它表示已确认跳过或退回的问题归档；工作流推进时识别并跳过，不得自行新增归档。

2. 推进当前脚本：
   - 运行 `workflow.py status <date_dir>`。
   - 先看 `next_action` 字段决定下一步，避免重复展开目录和日志。
   - 如果 `py/` 为空，运行 `workflow.py advance <date_dir>`，从 `bak/` 按文件名顺序复制下一个未完成脚本到 `py/`。
   - `advance` 必须跳过已存在于 `done/**/*.py` 或 `ignore/**/*.py` 的同名脚本。
   - 若 `py/` 中已有一个脚本，继续处理它，不要自动覆盖。

3. 初修脚本：
   - 优先运行 `workflow.py lint <date_dir>` 获取短静态检查报告。
   - 读取「脚本生成器技能」。
   - 读取「红线问题文档」。
   - 以《脚本编写注意事项》的红线规则和通用检查清单为主导；编码参考只在不违反红线时采用。
   - 分析 `py/` 中当前脚本，修正命令、步骤、断言、teardown 和红线问题。
   - 本地运行 `py_compile`。如系统 Python 缓存受限，使用 `PYTHONPYCACHEPREFIX=/private/tmp/ruijie_pycache`。
   - 告诉用户将 `py/` 中当前脚本通过 RuijieWorkSpace 手动上传执行机执行。

4. 日志接收与 FAIL 回流：
   - 执行机输出或用户下载的日志统一放入用户已配置的外部接收目录；允许其中暂存多个日志，也允许不同子目录中存在同名日志。
   - 用户说明“最新日志已经更新”“日志已更新”“日志已上传”“最新日志已上传”“执行机日志已回流”等同义表达时，运行 `workflow.py receive-log <date_dir>`。
   - 若本文件尚未配置接收目录且旧状态文件也无兼容配置，停止并要求用户提供路径；确认目录存在后更新本文件配置，不要求用户执行命令。
   - 命令根据当前 `py/` 脚本名前缀递归匹配 `.txt`，按最后修改时间选择最新文件，移动到 `log/`，并删除 `log/` 中其他全部内容。
   - 若 `py/` 为空、存在多个脚本或接收目录中无匹配日志，命令必须停止，且不得清理现有 `log/`。
   - 接收成功后运行 `workflow.py status <date_dir>`，只读取 `log/` 中当前脚本唯一且最新的日志。
   - 优先运行 `workflow.py log-summary <date_dir>`，只读取 FAIL 关键片段；只有摘要不足以定位时才打开完整日志。
   - 如果最新日志是 FAIL，必须告诉用户错误原因；优先使用 `log-summary` 的 `error_reason` 和 `error_excerpt`，再结合当前脚本调用「脚本生成器技能」分析失败原因并修正脚本。
   - 修正后检查后续步骤是否可能继续失败，并回到手动远程执行。
   - FAIL 修复完成后保留当前 FAIL 日志，运行 lint 和 py_compile 后提示用户重新执行；不得主动删除旧 FAIL 日志。收到用户“最新日志已经更新”等通知后，才运行 `receive-log` 替换旧日志。
   - 如果用户明确确认该脚本无法解决、需要跳过，运行 `workflow.py ignore-current <date_dir>`。该命令只允许在当前脚本最新同前缀日志为新鲜 FAIL 时执行，会将当前 `.py`、最新 FAIL 日志和 `error.json` 归档到 `ignore/<脚本stem>/`，从 `py/` 移除当前脚本，并自动推进下一个 bak 脚本。

5. PASS 终验：
   - 如果最新同前缀日志是 PASS，读取 PASS 日志和当前脚本；日志早于脚本时给出警告，但不得因此拒绝终验或封包。
   - 优先运行 `workflow.py final-check <date_dir>` 获取短终验报告。
   - 检查日志步骤、脚本步骤和预期是否一致。
   - 检查是否真实论证 SRS 六级规格。
   - 检查脚本和日志中的 `@用例等级`，并结合 PASS 日志用时判断是否需要提升等级；日志用时超过 10 分钟时，L0/L1 必须提升到 L2。
   - 检查 `final-check` 的 `pass_anomaly_check`。如果 PASS 日志真实业务步骤中出现明显命令异常或 Python 异常关键字，必须说明命中行号、关键字和原因，禁止封包。
   - 检查每个 `with step.expect(...)` 块是否至少有一个有效 `assert`；`teardown_method` 的配置恢复步骤除外，该步骤只下发恢复命令，由框架配置对比检查恢复结果。
   - 严禁虚假断言，例如 `assert True`、只判断命令回显存在却不验证预期行为、日志为空却宣称统计记录生成、公共库函数返回 `FAIL` 却断言 `FAIL` 当作通过。
   - 检查「红线问题文档」和《脚本编写注意事项》：禁止 `exec()`、禁止含 `exec` 的命名、禁止 `python.skip/pytest.skip`、禁止 `usefixtures`、禁止自定义函数、禁止硬编码接口；非 teardown 配置命令必须 `cmd_list` + `check_write_cmd`，teardown 配置恢复使用 `cmd_list` + `command` 且不增加校验和断言；BGP instance 规则必须符合规格。
   - 如果终验需要改脚本，按当前策略允许继续使用原 PASS 日志封包，但必须提示脚本已晚于日志并继续完成其他终验检查。

6. 封包和推进下一个：
   - 终验通过后运行 `workflow.py package <date_dir>`。
   - `package` 不比较 PASS 日志与脚本的先后时间；脚本修改时间晚于日志时输出警告并继续封包。
   - 脚本会从当前 `.py` docstring 解析 `@srs一级规格`、`@srs二级规格`、`@srs三级规格`。
   - 默认封包到日期目录下：`<date_dir>/done/<一级>/<二级>/<三级>/`。
   - 终版 `.py` 放在规格目录下，最新 PASS 日志放到该目录的 `Log/` 子目录。
   - 封包脚本会在终版 `.py` 中删除 docstring 的 `@具体UI命令（脚本完成后删除）:` 行，并按日志用时修正归档脚本的 `@用例等级`，再从 `py/` 移除当前工作副本。
   - 如果当前脚本是锐捷三方退回后重新放入 `py/` 返工的脚本，且重新执行 PASS、终验通过，则 `package` 必须覆盖 `done/` 中旧的同名脚本，并在目标 `Log/` 中删除同脚本前缀的旧 PASS 日志后写入最新 PASS 日志，避免新旧日志并存。
   - 如确需封包到其他根目录，可显式使用 `workflow.py package <date_dir> --package-root <path>`。
   - 封包后自动从 `bak/` 复制下一个未完成脚本到 `py/`。如果没有下一个，报告当天队列完成。

7. 锐捷人工审核退回：
   - 当用户说明某些已封包脚本被锐捷人工审核退回时，必须要求用户用 JSON 数组明确提供脚本名和退回原因，格式如下：

```json
[
  {
    "pyName": "1111.py",
    "failReason": "错误的原因1"
  },
  {
    "pyName": "222.py",
    "failReason": "错误的原因2"
  }
]
```

   - 使用 `workflow.py reject-done <date_dir> --items-json '<JSON数组>'` 或 `--items-file <json文件>` 处理。
   - 命令会从 `done/**/*.py` 查找同名脚本，并取该脚本同目录 `Log/` 下同前缀最新 PASS 日志，复制到 `ignore/<脚本stem>/`。
   - 同时创建 `error.json`，把用户提供的 `failReason` 写入其中。
   - 默认只复制，不删除 `done/` 中原封包结果；如果后续需要移动或删除，必须由用户单独确认。

8. EXE Check：
   - 当用户发出“exe check”指令时，运行 `workflow.py exe-check <date_dir>`。
   - 命令递归查找 `done/` 下全部 `.py`、`.txt` 文件，并复制到同级 `check/`。
   - 所有文件直接放入 `check/` 根目录，不复制 `done/` 的规格目录和 `Log/` 目录；执行成功后 `check/` 中不得存在子目录或非 `.py/.txt` 文件。
   - 复制前先按文件名检查重名。不同 `done/` 子目录存在同名 `.py` 或 `.txt` 时，停止并报告全部冲突路径，不得清理现有 `check/`。
   - 重名检查通过后，先完整复制到临时目录；复制成功后才清空原 `check/` 并写入扁平文件，防止读取源文件失败时先丢失旧检查文件。
   - 命令输出源目录、目标目录、复制数量、清理的旧内容和每个文件的源/目标路径。

9. Ignore Excel：
   - 用户明确输入“生成ignore表”“生成ignore”“ignore报表”或“导出ignore”时执行；普通用户无需输入 Python 或 Node 命令。
   - 若 `ignore/` 不存在或没有用例目录，不生成 Excel，明确回复当前没有可导出的 ignore 记录。
   - 读取每个 `ignore/<脚本stem>/error.json` 的 `pyName` 和 `failReason`，校验 JSON、脚本文件和重复名称。任一记录无效时停止，保留已有报表并列出全部问题目录。
   - 使用 `scripts/ignore_report.mjs` 和 Codex 内置 spreadsheet artifact 运行时生成 Excel；执行前通过 `load_workspace_dependencies` 获取 Node 和 `node_modules`，在会话临时目录创建 `node_modules` 连接，并把该临时连接路径传给脚本。禁止安装依赖或使用 `openpyxl`、`xlsxwriter`、`pandas.ExcelWriter`。
   - Excel 固定两列：`py脚本名称`、`失败原因`。按脚本名排序，文本按纯文本写入，首行冻结并启用筛选，长失败原因自动换行。
   - `failReason` 含“回收”时归入“回收”，否则归入“卡点”。仅创建有数据的 Sheet；混合数据的 Sheet 顺序固定为“卡点”“回收”。空失败原因归入“卡点”，保留空值并在结果中警告数量，不得编造原因。
   - 输出路径为 `<date_dir>/<日期目录名>_失败_<卡点数+回收数>.xlsx`。生成过程中在临时目录渲染并检查全部 Sheet，成功后才替换同名报表；不得删除其他 `_失败_*.xlsx`。

## 输出要求

每次处理都要明确告诉用户当前状态：

- 当前处理的脚本名。
- 使用的最新日志名及其 PASS/FAIL 状态。
- 若拒绝终验，说明是前缀不匹配、无日志、最新日志 FAIL，还是其他终验门禁未通过；PASS 日志早于脚本不能单独作为拒绝原因。
- 若修改了脚本且继续使用较早的 PASS 日志，明确提示该时间关系，但不得因此拒绝封包。
- 若接收日志完成，说明选中的源日志、最后修改时间、移动后的 `log/` 路径及被清理的旧日志数量。
- 若封包完成，给出 `done/` 下规格目录和 `Log/` 目录路径。
- 若执行 exe check，给出 `check/` 路径和复制文件数量。
- 若生成 Ignore Excel，给出文件路径、总数量、卡点数量、回收数量、空失败原因数量和实际生成的 Sheet 名称。
