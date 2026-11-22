# 字段与计算方式

输入是一个 UTF-8 JSON 对象，可带 BOM，最大 256 KiB、合计 1,200 个数组条目。每组源行号从 1 起，指数组原始位置。时间使用带偏移的 ISO 文本，按上海时区转成分钟精度，所有区间为左闭右开。普通字符串 ID 保留前导零。`cutoff` 为事件统计截点，等于截点的事件纳入本班。

| 数据集 | 字段 | 含义 |
|---|---|---|
| `windows` | `rink`,`start`,`end`,`kind` | 场地、营业开始与结束、`paid` 收费或 `waived` 豁免 |
| `passes` | `pass`,`owner`,`rink`,`start`,`end`,`allowance` | 权益号、队伍、冰面、生效时段，本有效段可用分钟整数 |
| `events` | `booking`,`revision`,`timestamp`,`action` | 订单号、正整数修订号、发生时刻、动作 |
| `events` | `owner`,`rink`,`start`,`end` | 新订与改单必填。撤销只读身份键和修订信息 |
| `debit.rows` | `day`,`start`,`end`,`debit`,`cash`,`waived`,`disputed` | 本地自然日、分钟区间及分钟分解 |
| `debit.rows` | `windows`,`passes`,`peers`,`codes` | 决定结果的营业行、权益行、冲突订单行、原因码 |
| `balance.tally` | `booked`,`difference`,`voucher_difference` | 有效预订分钟、分钟差、券已用差 |
| `manifest` | `version`,`created_at`,`cutoff`,`elapsed_ms`,`input_sha256`,`result_sha256` | 规则版、封存时刻、结算截点、耗时和两种摘要 |

## 裁决

先以升序修订重放每单。版本号从 1 连续递增，发生时刻随版号单调不降。第一版必须新订，中间只接受改单，最后一版可撤销。重复版本内容相同时保留最早发生时间。同版内容相异时整单暂停。有效订单进入计分钟，取消和没有可识别分钟的单据进入复核清单。

以午夜、营业窗端点、同场同队券版本端点切分每单。每段只可得到一种营业分类。两个异类营业窗重叠，或多个权益同时有效，相交片段进入争议。两个预订共同占用同一冰面时，两单一并待核对。收费段依开场时间逐段使用权益预算，按源版本全班累计。该段余额不够的部分记自付，豁免不扣权益。相同时点以冰面和订单号稳定排序。

每个有效预订分钟拆为扣券、自付、豁免、争议。每券的核发分钟等于已用加剩余。复核账按单检查，另检查全场扣券总数和券已用总数。

凭证导出一个 JSON，内含原始交换文本、完整阶段结论及运行清单。`input_sha256` 对上传原字节计算，`result_sha256` 对排序字段的 UTF-8 JSON 结果计算。样例中的现金列是待自付分钟，价格由票务系统维护。

## 更正凭证

更正不改写已交接批次，而是在 `corrections` 表追加一条只读记录，并把更正后的完整记录按同一算式独立封存为新批次。

| 字段 | 含义 |
|---|---|
| `id`,`original_id`,`supersedes_id`,`corrected_id` | 更正号、原批次号、承接的上一版本号、更正后封存批次号 |
| `idempotency_key` | 幂等键。留空按「原批次 + 更正依据 + 理由」自动生成；同 `(original_id, idempotency_key)` 重试只返回同一凭证 |
| `reason`,`operator`,`status`,`created_at`,`reviewed_at` | 更正理由、经办人、`pending`/`effective` 状态、登记与复核时刻 |
| `manifest.basis_sha256` | 更正依据（更正后源文件）字节摘要 |
| `manifest.original_*`,`manifest.corrected_*` | 原批次与更正后批次各自的输入/结果摘要，用于相互核对 |
| `impact.tally` | 对账口径前后值与差额（仅列发生变化的口径） |
| `impact.bookings`,`impact.vouchers` | 逐单分钟、逐权益分钟额的新增/移除/变更 |
| `impact.issues` | 争议复核条目的新增与消解 |
| `impact.sources` | 源行级依据，按业务身份键配对，`modified` 同时保留 `before`/`after` 与双方源行号 |
| `impact.summary` | 上述各类变化的计数与更正前后是否对平 |

当前可用版本沿更正链取最近一份 `effective` 更正；没有生效更正时仍是原批次，`pending` 更正不顶替当前版本。`GET /api/corrections/{id}/export` 导出的 JSON 同时包含原批次、更正后批次、更正依据与影响，三批次以哈希相连。
