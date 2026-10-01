#!/usr/bin/env bash
# 过敏原代码并码（canonical code merge）专项演示
#
# 场景：历史声明中存在同义/旧版过敏原代码（casein、wheat），质控将其并到
# 当前规范代码（milk、gluten）；并码可串接（milk -> dairy）。演示：
#   1) 登记校验：自映射/别名目标/改指拒绝且不留记录，必填原因，同向重试幂等；
#   2) 查询代码：最终规范代码 + 并码沿革；
#   3) 实时溯源：同义项按最终规范代码合并去重，保留每条原始来源路径/声明版本；
#   4) 原料声明版本与已签发标签快照保留原值；
#   5) 下游批次与设备残留在并码后重算，并沿用全局事件流发布实际变化；
#   6) 并码使并发声明更正的预演凭据失效（不得出现混合状态）。
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "1. 登记原料（声明使用旧代码）与加工"
curl -s "$BASE/materials" -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-CASEIN", "allergens": ["casein"],
  "declared_at": "2026-01-01T00:00:00Z"}'
curl -s "$BASE/materials" -X POST -H 'Content-Type: application/json' -d '{
  "batch_id": "RM-WHEAT", "allergens": ["wheat"],
  "declared_at": "2026-01-02T00:00:00Z"}'
# EQ-1 先后两批，无有效清洗：OUT-2 继承 OUT-1 的设备残留
curl -s "$BASE/processings" -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P1", "equipment_id": "EQ-1",
  "start_utc": "2026-01-01T08:00:00Z", "end_utc": "2026-01-01T09:00:00Z",
  "inputs": ["RM-CASEIN"], "outputs": ["OUT-1"]}'
curl -s "$BASE/processings" -X POST -H 'Content-Type: application/json' -d '{
  "processing_id": "P2", "equipment_id": "EQ-1",
  "start_utc": "2026-01-02T08:00:00Z", "end_utc": "2026-01-02T09:00:00Z",
  "inputs": ["RM-WHEAT"], "outputs": ["OUT-2"]}'

say "2. 并码登记（旧代码 -> 规范代码，必填原因）"
curl -s "$BASE/allergen-codes/merges" -X POST -H 'Content-Type: application/json' -d '{
  "old_code": "casein", "target_code": "milk",
  "reason": "casein 为牛奶蛋白，按规范代码 milk 统一"}'

say "2b. 拒绝场景（HTTP 状态码；均不留记录）"
printf 'self-map (expect 400):        '
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/allergen-codes/merges" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"old_code":"milk","target_code":"milk","reason":"x"}'
printf 'blank reason (expect 422):    '
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/allergen-codes/merges" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"old_code":"a","target_code":"b","reason":"  "}'
# 先把 wheat 并到 gluten，再尝试把新代码并到别名 wheat
curl -s "$BASE/allergen-codes/merges" -X POST -H 'Content-Type: application/json' -d '{
  "old_code": "wheat", "target_code": "gluten", "reason": "小麦含麸质"}' >/dev/null
printf 'alias target (expect 409):    '
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/allergen-codes/merges" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"old_code":"spelt","target_code":"wheat","reason":"x"}'
printf 'repoint old code (expect 409):'
curl -s -o /dev/null -w ' %{http_code}\n' "$BASE/allergen-codes/merges" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"old_code":"casein","target_code":"dairy","reason":"改指拒绝"}'

say "2c. 同向重试幂等（返回原结果，不新增记录/事件）"
curl -s "$BASE/allergen-codes/merges" -X POST -H 'Content-Type: application/json' -d '{
  "old_code": "casein", "target_code": "milk", "reason": "重复提交不覆盖"}'

say "3. 串接：milk -> dairy；查询最终规范代码与沿革"
curl -s "$BASE/allergen-codes/merges" -X POST -H 'Content-Type: application/json' -d '{
  "old_code": "milk", "target_code": "dairy", "reason": "乳制品规范代码更名"}'
curl -s "$BASE/allergen-codes/casein/resolve"   # casein -> milk -> dairy
curl -s "$BASE/allergen-codes/merges"           # 全部并码登记

say "4. 实时溯源：OUT-2 合并同义项（含设备残留下游）并保留原始来源"
curl -s "$BASE/batches/OUT-2/trace"
# allergens 以最终规范代码为键（dairy/gluten，去重）；
# allergen_origins 保留每条来源的 declared_code（casein/wheat）、批次与声明版本；
# basis.material_declarations 中声明版本仍保留原始代码。

say "5. 原料声明保留原值，另附规范代码解释"
curl -s "$BASE/materials/RM-CASEIN"
# allergens=["casein"]（原值），canonical_allergens=["dairy"]（按当前并码解释）

say "5b. 并码前签发的标签快照保留旧代码（并码后不变）"
# （若在并码前签发）GET /labels/<id> 始终返回签发时冻结结论。

say "6. 受影响批次按全局事件流发布（op=allergen_code_merge，仅实际变化批次）"
CURSOR=$(curl -s "$BASE/changes/snapshot" | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"])')
# 此处仅演示读取口径；并码事件已在上面登记时产生（游标取并码前快照可完整观察）
curl -s "$BASE/changes/events?cursor=0&limit=100" | python3 -c '
import sys, json
for e in json.load(sys.stdin)["events"]:
    if e["op"] == "allergen_code_merge":
        print(e["seq"], e["batch_id"],
              e["op_ref"]["old_code"], "->", e["op_ref"]["target_code"],
              e["before"]["conclusion"]["allergens"], "=>",
              e["after"]["conclusion"]["allergens"])'

say "7. 并发声明更正：并码使旧预演凭据失效（提交 409，需重新预演）"
PV=$(curl -s "$BASE/materials/RM-WHEAT/declarations/preview" \
  -X POST -H 'Content-Type: application/json' \
  -d '{"allergens": ["rye"]}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["preview_id"])')
curl -s "$BASE/allergen-codes/merges" -X POST -H 'Content-Type: application/json' -d '{
  "old_code": "rye", "target_code": "gluten", "reason": "黑麦同样含麸质"}' >/dev/null
printf 'stale preview commit (expect 409): '
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/previews/$PV/commit"
