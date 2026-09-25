# L3B Architecture Record

Hệ thống dùng kiến trúc multi-agent rule-based, evidence-first. Không lưu prompt, API key,
chain-of-thought hoặc dữ liệu MCP thô vào trace.

## 1. System overview

```text
Input
  │
  ▼
Coordinator → Entity/Customer → Order/Product → Shipment → Payment/Refund → Policy
                    │                │             │             │             │
                    └────────────────┴─────────────┴─────────────┴─────────────┘
                                      case-scoped MCP evidence ledger
                                                     │
                                                     ▼
                                         Rule Engine → Verifier
                                              ▲           │
                                              └─ fallback ┘  (tối đa 1 vòng)
                                                          │
                                                          ▼
                                                   Output + Trace
```

`solve_case()` tạo state mới cho từng case. Specialist chỉ thu evidence theo tool permission
cố định; `rule_engine.py` xác định issue, responsibility, payment/shipment verdict và resolution.
Verifier normalize output, kiểm tra schema/provenance/cross-field consistency và dùng conservative
fallback nếu rule draft không hợp lệ.

`day09 run` không gọi Ollama, OpenRouter hoặc bất kỳ model provider nào. Pipeline chấm bài hoàn
toàn deterministic và không gửi case/evidence ra dịch vụ LLM.

## 2. Agent ownership và least privilege

| Actor | Trách nhiệm | Tool permission |
| --- | --- | --- |
| Coordinator | Tạo plan/hypothesis code, handoff hữu hạn | Không gọi evidence tool |
| Entity/customer | Xác minh candidate và customer history | `get_order`, `get_customer_history` |
| Order/product | Thu item, seller và product context | `get_order_items`; `get_product_context` theo product hypothesis |
| Shipment | Thu timeline và phân loại giao hàng | `get_shipment_summary` |
| Payment/refund | Đối soát capture và refund lifecycle | `get_payment_timeline`, `get_refund_timeline`; `get_order_payments` là fallback |
| Policy | Lấy chính sách có thẩm quyền | `get_policy` |
| Rule engine | Tổng hợp deterministic từ case memory | Không gọi MCP |
| Verifier | Schema, provenance và consistency | Không gọi MCP |

Mọi tool phải xuất hiện trong kết quả discovery. Không actor nào được tự đoán tool hoặc mở rộng
scope. Seller ID lấy từ authoritative order items nên không phát sinh một seller lookup trùng lặp.

## 3. Think, memory và loop

- **Think:** coordinator tạo hypothesis code từ claim; rule engine đánh giá lifecycle theo thứ tự
  refund → order status → payment → shipment → unsupported/insufficient. Đây là quyết định có cấu
  trúc, không phải chain-of-thought và không ghi nội dung suy luận riêng vào trace.
- **Memory:** `WorkflowState` giữ plan, hypothesis, entity resolution, evidence ledger, tool failure,
  draft và verification errors. State chỉ sống trong một lần `solve_case()`.
- **Loop:** verifier chạy tối đa hai attempt. Attempt đầu kiểm tra rule draft; nếu lỗi thì handoff về
  rule engine và dùng conservative fallback. Không gọi lại MCP trong vòng sửa.
- **Termination:** `MAX_VERIFICATION_ATTEMPTS=2`; specialist graph không có edge quay lại.

## 4. Entity resolution và A2A

Entity Agent gọi `get_order` đúng một lần cho từng candidate và gọi customer history đúng một lần
khi có hint. Candidate chỉ được chấp nhận nếu ID xuất hiện trong MCP order hoặc customer evidence:

- đúng một match: `resolved`, confidence 0.99;
- nhiều match: `ambiguous`, confidence 0.4;
- không match: `not_found`, confidence 0.0.

Mỗi stage emit `handoff` và `task_assigned` với `case_id` làm correlation key. Agent phụ thuộc order
không gọi tool nếu entity chưa resolve.

## 5. Evidence lifecycle

1. Compatible MCP client truyền `case_id` của state vào mọi call.
2. Public MCP envelope schema được validate trước khi workflow nhận evidence.
3. Ledger lưu nguyên `evidence_ref`, hash, domain, data và warnings theo cache key
   `(tool_name, normalized_arguments)`.
4. Mỗi evidence được sử dụng emit đúng một `tool_result_consumed` với actor, tool và ref.
5. Rule engine chỉ trích ref của tool liên quan đến issue/claim.
6. Normalizer loại ref không tồn tại trong ledger; không tự tạo hoặc sửa `evidence_ref`.
7. Memory bị hủy sau case nên evidence không thể chảy chéo case.

`mcp_gateway.py` gốc được giữ nguyên. `mcp_gateway_v2.py` là client mới cho MCP SDK 2.2.0 và
hỗ trợ cả field snake_case (`is_error`, `structured_content`) lẫn camelCase cũ. Server, tool,
request và public contract không thay đổi.

## 6. Rule precedence và arbitration

Rule engine ưu tiên dữ liệu có thẩm quyền theo thứ tự:

1. refund/payment lifecycle event;
2. order status và item totals;
3. shipment timeline/actor;
4. policy rule từ `get_policy`;
5. customer claim chỉ là hypothesis, không phải bằng chứng.

Các issue được phát hiện từ evidence gồm canceled/unavailable order đã thanh toán, seller hoặc
logistics delay, payment mismatch, duplicate charge, refund pending/failed, valid split payment và
unsupported claim. Refund chỉ lấy từ policy evidence rồi trừ khoản đã refund; tổng refund lines
luôn bằng `recommended_refund_brl`.

## 7. Failure và efficiency policy

| Failure | Retry | Fallback |
| --- | ---: | --- |
| MCP transport error | 1 | Fail-fast nếu order hoặc policy evidence vẫn thiếu |
| MCP generic/application execution error | 0 | Không retry lỗi nghiệp vụ hoặc dữ liệu không tồn tại |
| MCP application/schema error | 0 | Không giả lập data/ref |
| Entity ambiguous/not found | 0 | Bỏ các tool phụ thuộc order |
| Invalid rule draft | 1 verifier loop | Conservative `insufficient_evidence` output |
| Source conflict | 0 | Ghi `data_conflicts`, dùng lifecycle/policy precedence |

Independent calls trong cùng specialist chạy song song và cache ngăn gọi lặp. Decoy đã định danh
dạng `candidate-NNN` được ghi nhận là rejected nhưng không tiêu tốn call; các định dạng ID khác vẫn
được MCP xác minh để không overfit private set. Evidence planner lấy order, history, items, payment
timeline và policy làm lõi; product, shipment và refund timeline chỉ lấy khi hypothesis cần đúng
domain đó. `get_order_payments` chỉ chạy khi payment timeline không có capture event. Với bundle hiện
tại, planner dùng 5 hoặc 6 MCP calls mỗi case, trung bình 5.9; shipment verdict không tranh chấp có
thể suy ra từ delivery dates trong order evidence.
Chỉ timeout/lỗi transport được retry một lần với exponential backoff; generic tool error không retry.

## 8. Verification invariants

Trước finalize, verifier kiểm tra:

- output đúng L3B V2 schema, `case_id` và không có field ngoài contract;
- resolved status có đúng một order; not-found không chứa resolved order;
- mọi output/claim evidence ref thuộc ledger của đúng case;
- mọi input claim có đúng một assessment khi claim assessments được tạo;
- resolved order nằm trong affected entities;
- late seller là tập con của affected seller IDs;
- recommended refund bằng tổng refund lines và `no_action` không có refund dương;
- responsible party phù hợp issue/shipment/payment verdict;
- confidence trong `[0, 1]` và giảm khi thiếu evidence, entity mơ hồ hoặc timeline chưa đủ.

Public files trong `contracts/schemas/` là chân lý cao nhất và không bị workflow sửa đổi.

## 9. Reproducibility

- Python 3.11+, async state-machine thuần Python, không random sampling.
- Case chạy tuần tự; independent MCP calls trong một stage chạy đồng thời.
- Không cần model hoặc provider để chạy/validate/package.
- Lệnh: `day09 validate-inputs`, `day09 mcp-tools`, `day09 run`, `day09 validate`,
  `day09 package`.
