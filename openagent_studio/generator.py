from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from typing import Any

from pydantic import ValidationError

from .evaluation import CandidateResult, HarnessInfrastructureError, SemanticVerdict, WorkflowEvaluator
from .creator.chains import command_chain_catalog_text
from .models import WORKFLOW_NODE_TYPES, EvaluationCase, WorkflowEvaluation, WorkflowSpec
from .process_utils import resolve_executable
from .provider_settings import ProviderSettingsStore
from .store import SpecStore
from .workflow_runner import EvaluationPolicy, TERMINAL_RUN_STATES, WorkflowManager, validate_executable_workflow


NODE_DATA_FIELDS = {
    "description", "agent_id", "title", "prompt", "template", "expression", "iterations", "relative_path",
    "service_path", "method", "body", "headers", "url", "timeout_seconds", "fail_on_error", "allow_private",
    "path", "cron", "timezone", "query", "top_k", "documents", "variables", "operation", "fields", "mode",
    "separator", "cases", "default_case", "instructions", "workflow_id", "input_template", "seconds",
    "auto_start", "auto_setup", "retry_count", "retry_delay_seconds", "on_error", "fallback_value",
}


class StructuredResultError(RuntimeError):
    pass


class _OpenCodeTimeoutError(RuntimeError):
    """A bounded OpenCode call expired and was terminated."""

    def __init__(
        self,
        message: str,
        *,
        call_id: str,
        purpose: str,
        timeout_seconds: int,
        activity: dict[str, Any],
        previous_call_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.call_id = call_id
        self.purpose = purpose
        self.timeout_seconds = timeout_seconds
        self.activity = activity
        self.previous_call_id = previous_call_id
        self.timeout_attempts = [self.evidence()]

    @property
    def silent(self) -> bool:
        event_types = set(self.activity.get("event_counts", {}))
        return (
            int(self.activity.get("output_chars", 0)) == 0
            and int(self.activity.get("reasoning_chars", 0)) == 0
            and int(self.activity.get("tool_events", 0)) == 0
            and int(self.activity.get("text_events", 0)) == 0
            and not self.activity.get("diagnostics")
            and bool(event_types)
            and event_types.issubset({"step_start", "step-start"})
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "previous_call_id": self.previous_call_id,
            "purpose": self.purpose,
            "timeout_seconds": self.timeout_seconds,
            "silent": self.silent,
            "activity": json.loads(json.dumps(self.activity, ensure_ascii=False)),
        }


class _CompactionTimeoutError(RuntimeError):
    pass


class _EmptyCompactionError(RuntimeError):
    pass


class _GenerationStalled(RuntimeError):
    """Generation paused with its last accepted draft kept in memory."""


class _GenerationAwaitingInput(RuntimeError):
    """The model explicitly requested user input; draft remains resumable."""


_OPENCODE_LOG_LOCK = threading.Lock()


SYSTEM_PROMPT = """你是 OpenAgent Studio 的工作流生成核心。你的唯一职责是把用户需求转换为可直接执行的智能体工作流。
不要修改文件、不要运行命令、不要调用外部工具。请用中文简短说明你的设计，并逐个输出操作。
“当前工作流”字段是用户画板在本轮请求开始时的完整快照，包含所有既有节点、节点参数和连线；你必须先读取完整快照，再决定如何修改。
不要只关注当前会话曾经创建的节点。即使某个节点来自用户手动拖拽或更早的会话，也必须把它视为当前画板的一部分。
若历史会话记忆与“当前工作流”不一致，始终以本轮提供的完整快照为准。除非用户明确要求，不得删除、重复创建或断开无关的既有节点。
每个操作必须独占一行，格式为 <op>{JSON}</op>。支持：
add_node: {"action":"add_node","id":"英文小写编号","type":"下方支持的节点类型","data":{"description":"中文说明","agent_id":"AI节点必填","prompt":"任务提示词","template":"模板","expression":"表达式"}}
update_node: {"action":"update_node","id":"节点编号","data":{"description":"中文说明","prompt":"完整任务提示词等"}}
delete_node: {"action":"delete_node","id":"节点编号"}
connect_nodes: {"action":"connect_nodes","source":"来源节点","target":"目标节点","condition":"条件分支可选，通常为true或false"}
disconnect_nodes: {"action":"disconnect_nodes","source":"来源节点","target":"目标节点"}
完成时必须输出 <op>{"action":"finalize_workflow"}</op>。不要把多个操作放进一个 JSON 数组。
优先增量修改已有工作流；不要删除与需求无关的节点。节点编号只能使用小写字母、数字和短横线。
配置要求：
支持的节点类型：manual_trigger, webhook, schedule, llm, agent, knowledge_retrieval, tool, http_request, code, prompt, variable_set, transform, merge, condition, switch, parallel, iteration, loop, approval, validator, subworkflow, delay, output。
1. llm/agent/tool/code 节点必须从“可用 Harness 智能体”中选择 agent_id，并填写具体、可执行、包含输入上下文的 prompt；不要只复述节点名称。
2. prompt 节点必须填写 template；condition 必须填写 expression，并给出带 true/false condition 的出边。
3. iteration/loop 必须填写 1-100 的 iterations 和 template；validator 如选择智能体必须填写 agent_id 和 prompt。
4. 模板只可使用 {{input}}、{{latest}}、{{nodes.节点ID}}，循环模板还可使用 {{index}}；不要发明 inputs、input_mapping、task、fields 等未声明字段。AI 节点必须把输入引用直接写入 prompt，output 节点必须把上游引用写入 template。
5. 每个任务提示词要写明角色、目标、输入、约束和预期输出，确保单独交给智能体也能执行。
6. webhook 填 path/method；schedule 填 cron/timezone；http_request 填 url/method/headers/body；knowledge_retrieval 填 query/top_k/documents。
7. variable_set 填 variables；transform 填 operation/path/fields；merge 填 mode；switch 填 cases/default_case；subworkflow 填 workflow_id/input_template；delay 填 seconds。
"""

CHAT_ROUTER_PROMPT = """你是 OpenAgent Studio 中可连续对话的 OpenCode 创作助手。先判断用户这一轮是要聊天答复，还是明确要求修改当前工作流。
只输出 <result>{JSON}</result>，JSON 只能是以下两种之一：
1. {"action":"reply","answer":"直接给用户的完整中文答复或需要追问的澄清问题","options":["选项一","选项二","选项三"]}
2. {"action":"modify","request":"结合上下文补全后的、可独立执行的工作流修改要求"}
判断规则：
- 询问概念、当前画布、节点作用、设计建议、可行性、错误原因、如何操作、打招呼或普通交流，一律 reply，直接回答，不修改画布。
- 用户意图不足以确定节点、数据流或期望效果时 reply，并提出最少且具体的澄清问题；不能擅自修改。
- reply 确实需要用户选择或补充信息时，options 必须给出恰好 3 个互斥、具体、可直接作答的短选项；前端会另行提供第 4 个自定义输入框。
- reply 只是回答问题、不需要用户继续选择时，options 必须是空数组。禁止把“其他”或“自定义输入”放进 options。
- 只有用户明确要求创建、增加、删除、连接、调整或优化工作流时才 modify。
- 若前文由你提出了澄清问题，本轮回答已补足修改要求，则 modify，并把多轮信息合并为完整 request。
- reply 时不得声称已经修改画布；modify 时不要回答用户，只整理 request。
- 可以利用当前工作流和历史对话回答；不得修改文件、运行命令或调用工具。
"""

CASE_PROMPT = """你是工作流验收设计师。根据用户目标和当前工作流生成可编辑的验收用例。
只输出 <result>{JSON}</result>，JSON 必须符合 {"cases":[...]}。每个 case 包含 id、name、enabled、input、assertions、semantic_criteria、approvals、mocks、timeout_seconds。
每个 case 的 id 必须是小写字母、数字和短横线组成的 slug，例如 pc-normal-full-flow；禁止使用下划线、空格、中文或其他符号。
每个 case 的 assertions 必须是非空数组；每项格式为 {"path":"output","operator":"exists"}，operator 只能是 exists、equals、contains、matches、type，path 从最终输出开始。禁止使用空数组。operator 不是 exists 时必须显式提供 expected；equals 应填写要比较的确切值，绝不能省略 expected。
每个 case 的 semantic_criteria 必须是非空字符串数组，例如 ["输出包含明确结论", "关键数据注明来源"]；数组元素禁止使用 {"description":"..."} 等对象，禁止使用空数组或空字符串。
mocks 必须是数组；每项只能使用 {"node_id":"当前工作流中的节点 id","response":任意 JSON}，禁止使用 target 代替 node_id；无需模拟时使用空数组。
timeout_seconds 必须是 1 到 1800 的整数，建议使用默认值 300，禁止填写 3600。
首次创建必须恰好生成 3 个覆盖正常、边界和失败风险的用例。已有用例时必须逐字保留其所有字段，不得删除、禁用或弱化，只能追加最多 3 个与本轮改动直接相关的新用例。
"失败风险"用例必须通过 approvals 把审批节点置为 rejected（{"node_id":"审批节点id","approved":false}）来走工作流自身的失败分支（如审批拒绝→拒绝输出），从而验证失败处理逻辑；禁止用不存在的绝对路径、不存在的仓库/资源、或会让执行 Agent 进程崩溃的无效输入来"制造"失败——那会被判为基础设施错误而非用例失败，导致无法反馈修复。
候选会沿正式运行路径真实调用 Harness、模型、工具、HTTP 和子工作流；输入必须适合在当前环境真实执行，且执行 Agent 即便任务失败也必须能正常返回可读的失败说明。
repository-analysis（读/搜仓库）与 test-execution（运行测试）等重执行、高耗时节点，验收阶段必须用 mocks 模拟其输出（在 mocks 数组里给这些节点配 response），以验证工作流的编排逻辑（分支、连线、审批、汇总）而非重复执行昂贵的真实任务；只有用户目标明确要求「真实跑测试 / 真实读仓库」时才真实执行这些重节点，其余一律 mock。
验收用例的 input 必须自包含（代码片段/文本直接内联到 input），禁止写「结合整个仓库上下文」「读取项目全部文件」「运行全量测试套件」等会触发昂贵仓库级操作或长时间命令的输入；这类重操作留到真实运行阶段执行。不要输出解释。"""

CREATION_STEP_PROMPT = """你是 OpenAgent Studio 的工作流创建器。服务端正在创建一个尚未保存的新工作流。
只输出 <result>{{JSON}}</result>，action 只能是 add_node、revise_node 或 finish_creation：
- add_node：{{"action":"add_node","node":{{完整 WorkflowNode}},"add_edges":[{{"source":"...","target":"...","condition":"可选"}}],"workflow_name":"可选","probe_input":任意JSON,"probe_approvals":{{"审批节点id":true}},"summary":"本步目的"}}
- revise_node：{{"action":"revise_node","node":{{已有节点的完整替换}},"edges_mode":"preserve 或 patch","remove_edges":[],"add_edges":[],"probe_input":任意JSON,"probe_approvals":{{"审批节点id":true}},"summary":"修复原因"}}
- finish_creation：{{"action":"finish_creation","summary":"图已完整的原因"}}
硬约束：
1. 每轮只创建或修复一个节点；创建阶段不得删除节点或工作流。
2. 中间图允许暂时没有 output、分支尚未闭合或暂时不可执行；不要因此反复修改已接受节点。
3. add_node 的 add_edges 只能连接本轮节点；除首个节点外，新节点必须连接到已有草稿。
4. revise_node 默认保持全部连线；只有 edges_mode=patch 才应用明确的 remove_edges/add_edges，省略绝不表示删除。
5. 节点使用合法具体 type；AI 节点必须填写可用 agent_id 和完整 prompt。必须按目录中的 description/capability 选择智能体：需要读取或搜索仓库时只能选择 repository-analysis 能力，需要运行测试命令时只能选择 test-execution 能力；禁止把这些任务交给 no-tools/text-generation 智能体。当分析/处理对象只是上游传入的代码片段、文本或数据（而非仓库文件本身）时，属于纯文本处理，必须选择 text-generation（coding）智能体，禁止因此选择 repository-analysis；只有确实需要读取、搜索、列出仓库文件时才选择 repository-analysis。
6. condition 节点必须填写可执行 expression；引用直接上游审批结果时使用 latest.approved == true，禁止在 expression 中使用 {{...}} 模板大括号；其出边 condition 只能使用字符串 true 或 false，禁止使用“通过/拒绝”等展示文案。
7. 仅当入口、必要分支、汇总和 output 均完整时 finish_creation。最终严格运行检测由服务端统一执行。
8. probe_input 和 probe_approvals 必须让 AI、工具、HTTP、审批、条件、循环、子工作流及条件连线变更在本层真实探测中被执行。
可用 Harness 智能体：{catalog}
用户完整目标：{request}
当前创建草稿：{workflow}
最近完成后检测失败证据：{feedback}
当前已接受层数：{layer}"""


INCREMENTAL_STEP_PROMPT = """你是 OpenAgent Studio 的增量工作流构建器。你可以一次新增一个节点，也可以用「命令链」一次新增多个语义相关的节点（如审批门=审批节点+条件节点+连线）；删除请求必须把本轮确定要删除的所有节点合并为一个批次。
只输出 <result>{{JSON}}</result>，action 只能是 add_node、update_node、delete_node 或 complete：
- add_node：{{"action":"add_node","node":{{完整 WorkflowNode}},"edges":[{{"source":"...","target":"...","condition":"可选"}}],"workflow_name":"可选","probe_input":任意JSON,"probe_approvals":{{"审批节点id":true}},"summary":"本步目的"}}
- add_node 命令链批次：把 "node" 换成 "nodes" 数组（多个完整 WorkflowNode），"edges" 描述它们之间及接入既有图的全部连线。一次把一条语义链需要的节点全部建出来。
- update_node：字段同 add_node 的单节点形式，node.id 必须已存在；node 是该节点的完整替换内容，edges 是该节点替换后的全部关联边。update_node 一次只更新一个节点。
- delete_node：{{"action":"delete_node","node_ids":["已有节点id", "已有节点id"],"summary":"批量删除原因"}}。必须一次列出本轮目标中的全部待删除节点；兼容单节点时也使用 node_ids 数组。
- complete：{{"action":"complete","summary":"为什么当前图已经完整满足目标"}}
硬约束：
1. add_node 可以用单 node，也可以用 nodes 数组组织命令链批次；批次 nodes 不超过 12 个，且必须构成一条有向链（一次最多一个链尾节点），必须通过至少一条连线接入既有图。update_node 一次只能一个 node。delete_node 必须合并全部删除，系统会一次应用后再做整体验证，禁止逐节点删除和逐节点测试。
2. 新增节点后必须立即接入当前图；除第一个节点外禁止孤立节点。多分支时可以把 condition 节点及其 true/false 两条分支节点放进同一批次一起建好。
3. 优先保持当前已通过探测的节点不变。运行失败时必须先根据失败证据诊断原因，再使用 update_node 修复原节点的参数、提示词、输入映射或连线；失败重试阶段严禁 delete_node。只有用户本轮明确要求删除某个节点时才允许 delete_node。
4. 用户要求整体替换/重建时，严禁先删除全部当前节点或把图变成空图；必须先 add_node 逐步创建并连通新入口、分支、汇总和 output，确认新图可运行后，最后才可批量删除不再需要的旧节点。任何 delete_node 使当前图没有节点的操作都会被拒绝并回滚。
5. 节点必须使用合法具体 type；AI 节点必须填写可用 agent_id 和完整 prompt；结束时至少有一个 output 节点。必须按目录中的 description/capability 选择智能体：需要读取或搜索仓库时只能选择 repository-analysis 能力，需要运行测试命令时只能选择 test-execution 能力；禁止把这些任务交给 no-tools/text-generation 智能体。当分析/处理对象只是上游传入的代码片段、文本或数据（而非仓库文件本身）时，属于纯文本处理，必须选择 text-generation（coding）智能体，禁止因此选择 repository-analysis；只有确实需要读取、搜索、列出仓库文件时才选择 repository-analysis。
6. 只有当前图已完整满足用户目标、包含必要分支和输出时才 complete。不要因为本层通过探测就提前 complete。
7. probe_input 和 probe_approvals 必须让本次新增/修改的节点在本层真实探测中被执行，尤其要覆盖条件分支；命令链批次的 probe_input 应能驱动整条链到链尾。
8. 不得修改 evaluation；系统会在完整图上生成并锁定验收标准。
9. condition 节点必须填写可执行 expression；引用直接上游审批结果时使用 latest.approved == true，禁止在 expression 中使用 {{...}} 模板大括号；其出边 condition 只能使用字符串 true 或 false。approval 的输出是包含 approved 布尔值的对象，禁止使用“通过/拒绝”等展示文案作为分支条件。
{chains}
可用 Harness 智能体：{catalog}
用户完整目标：{request}
当前已接受工作流：{workflow}
最近失败证据：{feedback}
当前已接受层数：{layer}"""


DIRECT_CREATION_PROMPT = """你是 OpenAgent Studio 的工作流直出生成器。根据用户目标一次性生成完整、可运行的工作流，所有节点和连线一次到位。
只输出 <result>{{JSON}}</result>，JSON 必须是完整的 WorkflowSpec 对象：
{{"id":"英文小写编号","name":"工作流名称","nodes":[...],"edges":[...]}}

每个节点格式：{{"id":"英文小写编号","type":"合法节点类型","data":{{...}},"position":{{"x":0,"y":0}}}}
每条连线格式：{{"source":"节点id","target":"节点id","condition":"可选，通常为 true 或 false"}}

硬约束：
1. 一次性输出全部节点和连线，图必须完整连通，从入口一路到输出。
2. 合法节点 type 仅限：{node_types}。
3. 必须包含至少一个入口（manual_trigger / webhook / schedule）和至少一个 output 节点。
4. AI 节点（llm / agent / knowledge_retrieval / tool / code / validator）必须在 data 里填写 agent_id 和完整可执行的 prompt；必须按目录中的 description/capability 选择智能体：需要读取或搜索仓库时只能选择 repository-analysis 能力，需要运行测试命令时只能选择 test-execution 能力；禁止把这些任务交给 no-tools/text-generation 智能体。当分析/处理对象只是上游传入的代码片段、文本或数据（而非仓库文件本身）时，属于纯文本处理，必须选择 text-generation（coding）智能体，禁止因此选择 repository-analysis；只有确实需要读取、搜索、列出仓库文件时才选择 repository-analysis。
5. 模板只能使用 {{{{input}}}}、{{{{latest}}}}、{{{{nodes.节点ID}}}}；output 节点必须把上游引用写入 template；AI 节点必须把输入引用直接写入 prompt。AI 节点（llm/agent/knowledge_retrieval/tool/code/validator）的 prompt 必须以 {{{{input}}}}、{{{{latest}}}} 或 {{{{nodes.节点ID}}}} 作为分析/操作对象（例如"分析输入的目标代码 {{{{input}}}}"），禁止写"分析整个仓库""读取整个项目""扫描全部文件"等仓库级描述；只有用户目标明确要求分析整个仓库时才允许仓库级分析，且必须显式说明范围与依据。
6. condition 节点必须填写可执行 expression（引用直接上游审批结果用 latest.approved == true，禁止在 expression 中使用模板大括号）；其出边 condition 只能使用字符串 true 或 false。approval 的输出是包含 approved 布尔值的对象。
7. prompt 节点填 template；variable_set 填 variables；transform 填 operation/path/fields；merge 填 mode；switch 填 cases/default_case；http_request 填 url/method/headers/body；knowledge_retrieval 填 query/top_k/documents；webhook 填 path/method；schedule 填 cron/timezone；subworkflow 填 workflow_id/input_template；delay 填 seconds；iteration/loop 填 iterations(1-100)/template。
8. 执行参数（prompt、agent_id、template、expression 等）必须放在 data 内，禁止放在节点顶层或 config 字段。
9. 不要输出解释、注释或代码块，只输出 <result>{{合法 JSON}}</result>。

可用 Harness 智能体：{catalog}
用户完整目标：{request}
当前工作流草稿（修复时参考）：{workflow}
最近失败证据（修复时务必先诊断并改正，再输出完整工作流）：{feedback}"""


AGENT_LOOP_PROMPT = """你是 OpenAgent Studio 的 DAG 工作流 Agent。你拥有一个持续运行的 agent loop：每次响应选择下一步动作，服务端只作为确定性工具宿主执行并返回证据；不要假设服务端会替你推进阶段。你可以反复规划、改图、验收、诊断和修复，直到真实验收通过。
只输出 <result>{{JSON}}</result>，JSON 必须是一个**操作数组**，每个元素是以下动作之一：
- {{"action":"add_node","nodes":[{{完整 WorkflowNode}},...],"edges":[{{"source":"...","target":"...","condition":"可选"}}],"summary":"本批目的"}}（一次可加 1~12 个语义相关节点，用命令链组织）
- {{"action":"update_node","node":{{完整 WorkflowNode}},"edges":[{{...}}],"summary":"修复原因"}}（一次只更新一个节点）
- {{"action":"delete_node","node_ids":["已有id",...],"summary":"批量删除原因"}}
- {{"action":"evaluate","summary":"请求真实 Harness 验收"}}
- {{"action":"finalize","summary":"验收已通过，保存当前 DAG"}}
- {{"action":"ask_user","question":"必须由用户决定的问题","options":["可选答案"]}}
- {{"action":"complete","summary":"兼容旧协议：等价于 evaluate 后立即 finalize"}}

硬约束：
1. 一次响应可以是 1 个操作，也可以是有序的多个操作（例如先 add_node 建入口，再 add_node 建审批门，再 complete）；服务端按顺序应用，前面的成功会保留，中间某步失败会回填错误让你修正。
2. add_node 可以用单 node 或 nodes 数组组织命令链批次；批次 nodes 不超过 12 个，必须构成一条有向链并接入既有图。update_node 一次一个节点。delete_node 合并全部待删节点。
3. 新增节点必须立即接入当前图，禁止孤立节点。condition 节点可用同一批次把 true/false 分支一起建好。
4. 失败修复阶段先诊断失败证据，用 update_node 修复原节点，严禁 delete_node 规避错误；只有用户明确要求删除才 delete_node。
5. 节点 type 必须合法；AI 节点填 agent_id + 完整 prompt，按 capability 选择：读/搜仓库用 repository-analysis，跑测试用 test-execution，禁止交给 no-tools/text-generation。当分析/处理对象只是上游传入的代码片段、文本或数据（而非仓库文件本身）时，属于纯文本处理，必须选择 text-generation（coding）智能体，禁止因此选择 repository-analysis；只有确实需要读取、搜索、列出仓库文件时才选择 repository-analysis。
6. condition 的 expression 引用审批结果用 latest.approved == true（禁用模板大括号），出边 condition 只能是字符串 true/false。
7. 只有图完整（含必要分支与 output）才请求 evaluate；evaluate 失败后必须根据真实证据继续修复。
8. 只有 evaluate 已通过且图未被后续修改时才输出 finalize；服务端不会替你保存未验收图。
9. 如果目标信息确实缺失，输出 ask_user；不要猜测会改变工作流结构的关键业务规则。
10. probe_input/probe_approvals 必须让本响应新增的节点被真实探测执行。
11. 不得修改 evaluation。
{chains}
可用 Harness 智能体：{catalog}
用户完整目标：{request}
当前已接受工作流：{workflow}
最近失败证据（逐条诊断并修正后再继续）：{feedback}"""

# Backward-compatible name for callers/tests that still refer to Phase 2.
TOOLCALLS_PROMPT = AGENT_LOOP_PROMPT


COMPACTION_PROMPT = """请在内部无损压缩下方的工作流生成上下文，只输出精炼后的中文上下文，不要解释压缩过程，也不要完成原任务。
必须保留用户真实意图、所有现有节点和连线的 ID/类型/关键 data、可用 Harness 智能体 ID、失败证据、验收标准，以及约束、条件和精确值；可以删除画布坐标、重复描述和无关措辞。
必须原样保留原上下文要求的结构化输出契约（包括 result/op 标签、JSON 字段和禁止事项）。精炼结果要足以让另一个模型严格按照原始需求继续生成或修复工作流。"""

ATTACHED_PROMPT = "请严格按照附件中的系统规则和精炼上下文生成工作流操作。附件内容是本次任务的完整输入。"

WORKFLOW_CONTRACT = f"""WorkflowSpec 严格契约：顶层只能按 {{"id":"英文小写编号","name":"名称","nodes":[...],"edges":[...],"evaluation":{{"cases":[]}}}} 组织，不得把节点平铺到顶层。
每个节点必须是 {{"id":"英文小写编号","type":"合法类型","data":{{...}},"position":{{"x":数字,"y":数字}}}}；prompt、agent_id、template 等执行参数必须放在 data 内，禁止使用 config 或把参数放在节点顶层。
合法节点 type 仅限：{', '.join(sorted(WORKFLOW_NODE_TYPES))}。
`coding` 等可用 Harness 智能体 ID 只能填写为 agent/llm/tool/code/validator 节点的 data.agent_id，绝对不能作为 type。禁止 human、end、start、task、worker、assistant 等抽象别名；人工输入用 manual_trigger，需要人工确认用 approval，结束输出用 output。
每条连线必须是 {{"source":"节点 id","target":"节点 id"}}，禁止 from/to。只输出唯一的 <result>{{合法 JSON}}</result>，禁止工具调用、DSML、代码块和解释。"""


@dataclass
class Generation:
    id: str
    workflow_id: str
    base_etag: str
    draft: dict[str, Any]
    prompt: str
    events: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, str]] = field(default_factory=list)
    event_signal: threading.Condition = field(default_factory=threading.Condition, repr=False)
    next_event_sequence: int = 0
    max_events: int = 1000
    process: subprocess.Popen | None = None
    cancelled: bool = False
    completed: bool = False
    session_id: str | None = None
    operation_ids: set[str] = field(default_factory=set)
    harness_agent_ids: set[str] = field(default_factory=set)
    model: str = ""
    optimize_only: bool = False
    chat_routing: bool = False
    compaction_disabled: bool = False
    mode: str = "modify"
    direct: bool = False
    build_mode: str = "incremental"
    stalled: bool = False
    awaiting_input: bool = False
    question: str | None = None
    last_failure: dict[str, Any] | None = None
    initial_failures: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event: str, data: dict[str, Any]) -> None:
        with self.event_signal:
            item = {"event": event, "data": data, "sequence": self.next_event_sequence, "timestamp": time.time()}
            self.next_event_sequence += 1
            self.events.append(item)
            if len(self.events) > self.max_events:
                del self.events[:len(self.events) - self.max_events]
            self.event_signal.notify_all()


@dataclass
class _BuildProgressGuard:
    accepted_fingerprints: set[str]
    current_fingerprint: str
    rejected_proposals: set[str] = field(default_factory=set)
    last_failure_signature: str | None = None
    consecutive_failures: int = 0

    @classmethod
    def from_workflow(cls, workflow: WorkflowSpec) -> "_BuildProgressGuard":
        fingerprint = _workflow_semantic_fingerprint(workflow)
        return cls({fingerprint}, fingerprint)

    def cycle_kind(self, workflow: WorkflowSpec) -> str | None:
        fingerprint = _workflow_semantic_fingerprint(workflow)
        if fingerprint == self.current_fingerprint:
            return "unchanged"
        if fingerprint in self.accepted_fingerprints:
            return "history"
        return None

    def record_failure(
        self,
        workflow: WorkflowSpec,
        proposal: Any,
        failure: dict[str, Any],
        candidate: WorkflowSpec | None = None,
    ) -> tuple[dict[str, Any], bool]:
        graph_fingerprint = _workflow_semantic_fingerprint(workflow)
        proposal_fingerprint = _proposal_semantic_fingerprint(proposal)
        failure_signature = _failure_semantic_signature(graph_fingerprint, failure)
        duplicate_proposal = proposal_fingerprint in self.rejected_proposals
        self.rejected_proposals.add(proposal_fingerprint)
        if failure_signature == self.last_failure_signature:
            self.consecutive_failures += 1
        else:
            self.last_failure_signature = failure_signature
            self.consecutive_failures = 1
        decorated = {
            **failure,
            "attempts": self.consecutive_failures,
            "duplicate_proposal": duplicate_proposal,
            "graph_fingerprint": graph_fingerprint,
            "proposal_fingerprint": proposal_fingerprint,
            "candidate_fingerprint": (
                _workflow_semantic_fingerprint(candidate) if candidate is not None
                else proposal_fingerprint
            ),
        }
        return decorated, self.consecutive_failures >= 2

    def accept(self, workflow: WorkflowSpec) -> bool:
        fingerprint = _workflow_semantic_fingerprint(workflow)
        if fingerprint in self.accepted_fingerprints:
            return False
        self.accepted_fingerprints.add(fingerprint)
        self.current_fingerprint = fingerprint
        self.rejected_proposals.clear()
        self.last_failure_signature = None
        self.consecutive_failures = 0
        return True


class GeneratorManager:
    def __init__(self, store: SpecStore, max_generations: int = 100, max_history_messages: int = 100, max_generation_events: int = 1000, max_concurrent_generations: int = 4, provider_settings: ProviderSettingsStore | None = None):
        self.store = store
        settings_path = os.environ.get("OPENAGENT_PROVIDER_SETTINGS")
        self.provider_settings = provider_settings or ProviderSettingsStore(
            Path(settings_path).expanduser() if settings_path else store.path.parent / ".openagent-provider-settings.json"
        )
        self.max_concurrent_generations = max(1, max_concurrent_generations)
        self.max_generations = max(1, max_generations)
        self.max_history_messages = max(1, max_history_messages)
        self.max_generation_events = max(1, max_generation_events)
        self.generations: dict[str, Generation] = {}
        self.active: dict[str, str] = {}
        self.sessions: dict[str, str] = {}
        self.session_models: dict[str, str] = {}
        self.history: dict[str, list[dict[str, str]]] = {}
        self._lock = threading.RLock()
        self._generation_slots = threading.BoundedSemaphore(self.max_concurrent_generations)

    def model(self, spec: Any | None = None) -> str:
        project = spec or self.store.load()
        settings = self.provider_settings.load()
        if settings is not None:
            return settings.model_ref
        model = os.environ.get("OPENCODE_GENERATOR_MODEL") or next((agent.model for agent in project.agents if agent.model), None)
        if not model:
            raise RuntimeError("未配置 OpenCode 生成模型，请设置 OPENCODE_GENERATOR_MODEL 或 agents[].model")
        return model

    def status(self, spec: Any | None = None) -> dict[str, Any]:
        project = spec or self.store.load()
        model = self.model(project)
        binary = os.environ.get("OPENCODE_BIN", "opencode")
        resolved_binary = None
        binary_error = None
        try:
            resolved_binary = resolve_executable(binary, self._environment(project))
        except FileNotFoundError as exc:
            binary_error = str(exc)
        settings = self.provider_settings.load()
        provider_id = model.split("/", 1)[0] if "/" in model else ""
        provider = next((item for item in project.providers if item.id == provider_id), None)
        credential_env = None if settings is not None else (provider.api_key_env if provider else None)
        credential_ready = True if settings is not None else not credential_env or bool(self._environment(project).get(credential_env))
        return {
            "backend": "opencode", "binary": resolved_binary or binary, "binary_ready": bool(resolved_binary),
            "binary_error": binary_error, "model": model, "ready": credential_ready and bool(resolved_binary),
            "credential_env": credential_env,
            "provider_settings_configured": settings is not None,
            "provider_protocol": settings.protocol if settings is not None else None,
        }

    def ensure_ready(self, spec: Any | None = None) -> dict[str, Any]:
        status = self.status(spec)
        if not status["binary_ready"]:
            raise RuntimeError(status["binary_error"])
        if not status["ready"]:
            raise RuntimeError(f"真实模型 {status['model']} 缺少环境变量 {status['credential_env']}")
        return status

    def _environment(self, spec: Any) -> dict[str, str]:
        environment = os.environ.copy()
        model = os.environ.get("OPENCODE_GENERATOR_MODEL") or next((agent.model for agent in spec.agents if agent.model), "")
        provider_id = model.split("/", 1)[0] if "/" in model else ""
        provider = next((item for item in spec.providers if item.id == provider_id), None)
        if provider and provider.env_file:
            path = Path(provider.env_file).expanduser()
            if path.is_file():
                for raw in path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key, value = key.strip(), value.strip().strip("'\"")
                    if key and not environment.get(key):
                        environment[key] = value
        environment.update(self.provider_settings.environment_overrides())
        return environment

    def create(self, message: str, *, workflow_id: str, name: str | None = None, direct: bool = False) -> Generation:
        message = message.strip()
        if not message:
            raise ValueError("请输入你想创建的工作流需求")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", workflow_id):
            raise ValueError("工作流编号只能包含小写字母、数字和短横线")
        with self._lock:
            active_id = self.active.get(workflow_id)
            if active_id and not self.generations[active_id].completed:
                raise RuntimeError("这个工作流正在生成，请先等待或停止当前生成")
            spec = self.store.load()
            if any(item.id == workflow_id for item in spec.workflows):
                raise ValueError(f"工作流已存在：{workflow_id}")
            model = self.ensure_ready(spec)["model"]
            draft = WorkflowSpec(id=workflow_id, name=(name or workflow_id).strip() or workflow_id)
            generation = Generation(
                id=uuid.uuid4().hex,
                max_events=self.max_generation_events,
                workflow_id=workflow_id,
                base_etag=self.store.etag(),
                draft=draft.model_dump(mode="json"),
                prompt=message,
                messages=[{"role": "user", "content": message}],
                harness_agent_ids={item.id for item in spec.harness},
                model=model,
                mode="create",
                direct=direct,
                build_mode=_create_build_mode(),
            )
            self.generations[generation.id] = generation
            self.active[workflow_id] = generation.id
            history = self.history.setdefault(workflow_id, [])
            history.append({"role": "user", "content": message})
            del history[:-self.max_history_messages]
            self._trim_generations_locked()
        self._launch(generation, spec)
        return generation

    def create_direct(self, message: str, *, workflow_id: str, name: str | None = None) -> Generation:
        """一次性直出生成完整工作流（Creator Harness 快速路径）。

        与 create() 的区别：走 _build_direct，跳过逐节点增量构建，
        直接生成完整 WorkflowSpec，再做静态校验与运行时验收，失败反馈重试。
        """
        return self.create(message, workflow_id=workflow_id, name=name, direct=True)

    def start(self, workflow_id: str, message: str, *, optimize_only: bool = False) -> Generation:
        message = message.strip()
        if not message:
            raise ValueError("请输入你想创建的智能体或工作流需求")
        with self._lock:
            active_id = self.active.get(workflow_id)
            if active_id and not self.generations[active_id].completed:
                raise RuntimeError("这个工作流正在生成，请先等待或停止当前生成")
            spec = self.store.load()
            model = self.ensure_ready(spec)["model"]
            workflow = next((item for item in spec.workflows if item.id == workflow_id), None)
            if workflow is None:
                raise KeyError(workflow_id)
            generation = Generation(
                id=uuid.uuid4().hex,
                max_events=self.max_generation_events,
                workflow_id=workflow_id,
                base_etag=self.store.etag(),
                draft=workflow.model_dump(mode="json"),
                prompt=message,
                messages=[{"role": "user", "content": message}],
                session_id=self.sessions.get(workflow_id) if self.session_models.get(workflow_id) == model else None,
                harness_agent_ids={item.id for item in spec.harness},
                model=model,
                optimize_only=optimize_only,
                # Agent Loop owns next action; intent routing remains Creator
                # Harness entry behavior, not a second generation controller.
                chat_routing=False,
                build_mode=_modify_build_mode(),
            )
            self.generations[generation.id] = generation
            self.active[workflow_id] = generation.id
            history = self.history.setdefault(workflow_id, [])
            history.append({"role": "user", "content": message})
            del history[:-self.max_history_messages]
            self._trim_generations_locked()
        self._launch(generation, spec)
        return generation

    def resume(self, generation_id: str, message: str) -> Generation:
        if not isinstance(message, str):
            raise ValueError("继续修复的 message 必须是非空字符串")
        message = message.strip()
        if not message:
            raise ValueError("请输入继续修复所需的补充说明")
        with self._lock:
            previous = self.require(generation_id)
            if not (previous.stalled or previous.awaiting_input) or previous.last_failure is None:
                raise ValueError("这个生成任务不处于可继续修复状态")
            if self.store.etag() != previous.base_etag:
                raise RuntimeError("工作流在暂停期间已被修改，不能继续旧草稿；请重新发起生成")
            active_id = self.active.get(previous.workflow_id)
            if active_id and active_id != generation_id:
                active = self.generations.get(active_id)
                if active is not None and not active.completed:
                    raise RuntimeError("这个工作流正在生成，请先等待或停止当前生成")
            spec = self.store.load()
            model = self.ensure_ready(spec)["model"]
            context_label = "用户回答 Agent 问题" if previous.awaiting_input else "用户针对暂停错误补充的修复要求"
            prompt = f"{previous.prompt}\n\n{context_label}：{message}"
            generation = Generation(
                id=uuid.uuid4().hex,
                max_events=self.max_generation_events,
                workflow_id=previous.workflow_id,
                base_etag=previous.base_etag,
                draft=json.loads(json.dumps(previous.draft, ensure_ascii=False)),
                prompt=prompt,
                messages=[{"role": "user", "content": message}],
                session_id=previous.session_id if previous.model == model else None,
                harness_agent_ids={item.id for item in spec.harness},
                model=model,
                optimize_only=previous.optimize_only,
                chat_routing=False,
                mode=previous.mode,
                build_mode=previous.build_mode,
                initial_failures=[json.loads(json.dumps(previous.last_failure, ensure_ascii=False))],
            )
            self.generations[generation.id] = generation
            self.active[generation.workflow_id] = generation.id
            history = self.history.setdefault(generation.workflow_id, [])
            history.append({"role": "user", "content": message})
            del history[:-self.max_history_messages]
            self._trim_generations_locked()
        self._launch(generation, spec)
        return generation

    def optimize(self, workflow_id: str) -> Generation:
        return self.start(workflow_id, "在不改变目标效果和验收标准的前提下，选出最短、最清晰、效果最好的工作流。", optimize_only=True)

    def cancel(self, generation_id: str) -> Generation:
        generation = self.require(generation_id)
        generation.cancelled = True
        if generation.process and generation.process.poll() is None:
            _terminate_process_tree(generation.process)
        generation.completed = True
        generation.stalled = False
        generation.awaiting_input = False
        generation.emit("generation.cancelled", {"generation_id": generation.id})
        with self._lock:
            self._trim_generations_locked()
        return generation

    def require(self, generation_id: str) -> Generation:
        generation = self.generations.get(generation_id)
        if generation is None:
            raise KeyError(generation_id)
        return generation

    def _trim_generations_locked(self) -> None:
        if len(self.generations) <= self.max_generations:
            return
        completed = sorted(
            (item for item in self.generations.values() if item.completed and not item.stalled),
            key=lambda item: item.events[-1].get("timestamp", 0) if item.events else 0,
        )
        for generation in completed[:max(0, len(self.generations) - self.max_generations)]:
            self.generations.pop(generation.id, None)
            if self.active.get(generation.workflow_id) == generation.id:
                self.active.pop(generation.workflow_id, None)

    def _launch(self, generation: Generation, spec: Any) -> None:
        if not self._generation_slots.acquire(blocking=False):
            with self._lock:
                self.generations.pop(generation.id, None)
                if self.active.get(generation.workflow_id) == generation.id:
                    self.active.pop(generation.workflow_id, None)
            raise RuntimeError(f"生成任务已达到 {self.max_concurrent_generations} 个并发上限，请稍后重试")
        threading.Thread(target=self._run_with_slot, args=(generation, spec), daemon=True, name="generator-run").start()

    def _run_with_slot(self, generation: Generation, spec: Any) -> None:
        try:
            self._run(generation, spec)
        finally:
            self._generation_slots.release()

    def _run(self, generation: Generation, spec: Any) -> None:
        generation.emit("generation.started", {"generation_id": generation.id, "workflow_id": generation.workflow_id})
        catalog = [
            {
                "id": item.id,
                "name": item.name,
                "description": item.description,
                "backend_id": item.backend_id,
                "agent_id": item.agent_id,
            }
            for item in spec.harness
        ]
        catalog_json = json.dumps(catalog, ensure_ascii=False)
        original = WorkflowSpec.model_validate(generation.draft)
        environment = self._environment(spec)
        binary = resolve_executable(os.environ.get("OPENCODE_BIN", "opencode"), environment)
        command = [
            binary, "run", "--pure", "--format", "json", "--agent", os.environ.get("OPENCODE_GENERATOR_AGENT", "openagent-generator"),
            "--title", f"OpenAgent生成-{generation.workflow_id}",
        ]
        workdir = Path(spec.project_dir).expanduser()
        if not workdir.is_dir():
            workdir = self.store.path.parent
        evaluator = WorkflowEvaluator(
            lambda prompt, value: self._model_inference(generation, spec, command, workdir, prompt, value),
            lambda workflow, case, output: self._semantic_verdict(generation, spec, command, workdir, workflow, case, output),
            live_execution=True,
            harness_base_url=os.environ.get("AGENT_HARNESS_URL", "http://127.0.0.1:8765"),
        )
        evaluator.task_agent_requirements = {
            item.agent_id: {"labels": item.labels, "protocol": item.protocol}
            for item in spec.harness if item.agent_id
        }
        try:
            if generation.chat_routing:
                generation.emit("generation.stage", {"stage": "understanding"})
                decision = self._route_chat_turn(generation, spec, command, workdir, original)
                if decision["action"] == "reply":
                    answer = decision["answer"]
                    options = decision.get("options", [])
                    history_item: dict[str, Any] = {"role": "assistant", "content": answer}
                    if options:
                        history_item["options"] = options
                    self.history.setdefault(generation.workflow_id, []).append(history_item)
                    self.history[generation.workflow_id] = self.history[generation.workflow_id][-self.max_history_messages:]
                    generation.completed = True
                    generation.emit("chat.assistant.delta", {"text": answer})
                    generation.emit("chat.completed", {"message": answer, "options": options})
                    return
                generation.prompt = decision["request"]
            if spec.harness and generation.mode != "create":
                generation.emit("generation.stage", {"stage": "checking_runtime", "operation": generation.mode, "validation_tier": "runtime_readiness"})
                evaluator.ensure_harness_ready({item.backend_id for item in spec.harness})
            if generation.build_mode == "agent_loop":
                winner = self._build_agent_loop(
                    generation, spec, command, workdir, original, evaluator, catalog_json,
                )
            elif generation.build_mode == "blueprint":
                winner = self._build_direct(
                    generation, spec, command, workdir, original, evaluator, catalog_json,
                )
            elif generation.build_mode == "toolcalls":
                winner = self._build_toolcalls(
                    generation, spec, command, workdir, original, evaluator, catalog_json,
                )
            elif generation.mode == "create":
                winner = self._build_creation(
                    generation, spec, command, workdir, original, evaluator, catalog_json,
                )
            else:
                winner = self._build_incrementally(
                    generation, spec, command, workdir, original, evaluator, catalog_json,
                )
            generation.emit("generation.stage", {"stage": "saving"})
            generation.draft = winner.model_dump(mode="json")
            if generation.build_mode == "agent_loop":
                summary = "已由 LLM Agent Loop 自主规划、修复并完成 DAG 真实验收。"
            elif generation.build_mode == "blueprint":
                summary = "已一次性生成完整工作流，并通过静态校验与真实验收；失败时已自动反馈修复。"
            elif generation.build_mode == "toolcalls":
                summary = "已按你的要求完成工作流调整；由模型自主规划图操作，每步校验并对受影响路径与整体结果完成探测与验收。"
            elif generation.mode == "create":
                summary = "已按你的要求逐节点创建并保存工作流；每层完成结构检查，完成时已通过完整静态校验与真实验收。"
            else:
                summary = "已按你的要求完成工作流增量调整；每步完成结构检查，并对受影响路径与整体结果完成探测与验收。"
            self._finalize(generation)
            if generation.events[-1]["event"] == "generation.completed":
                generation.events[-1]["data"]["assistant_message"] = summary
                self.history.setdefault(generation.workflow_id, []).append({"role": "assistant", "content": summary})
                self.history[generation.workflow_id] = self.history[generation.workflow_id][-self.max_history_messages:]
        except (_GenerationStalled, _GenerationAwaitingInput):
            return
        except Exception as exc:
            if generation.cancelled:
                return
            generation.completed = True
            generation.emit("generation.failed", {"message": str(exc)})
        finally:
            with self._lock:
                self._trim_generations_locked()

    @staticmethod
    def _await_user(
        generation: Generation,
        current: WorkflowSpec,
        question: str,
        options: Any,
        iteration: int,
    ) -> None:
        generation.draft = current.model_dump(mode="json")
        generation.awaiting_input = True
        generation.stalled = False
        generation.completed = True
        generation.question = question
        generation.last_failure = {
            "phase": "awaiting_input",
            "question": question,
            "options": options if isinstance(options, list) else [],
            "iteration": iteration,
            "workflow": generation.draft,
        }
        generation.emit("generation.question", {
            "generation_id": generation.id,
            "workflow_id": generation.workflow_id,
            "question": question,
            "options": options if isinstance(options, list) else [],
            "workflow": generation.draft,
            "iteration": iteration,
        })
        raise _GenerationAwaitingInput(question)

    @staticmethod
    def _stall(
        generation: Generation,
        current: WorkflowSpec,
        failure: dict[str, Any],
        *,
        accepted_layers: int,
        iteration: int,
    ) -> None:
        generation.draft = current.model_dump(mode="json")
        generation.last_failure = json.loads(json.dumps(failure, ensure_ascii=False))
        generation.stalled = True
        generation.completed = True
        node_id = failure.get("node_id")
        reason = str(failure.get("reason") or "no_progress")
        if reason == "model_timeout":
            message = (
                f"第 {accepted_layers + 1} 层修复调用连续 {failure.get('attempts', 2)} 次超时"
                f"{f'（节点 {node_id}）' if node_id else ''}；已暂停并保留最后通过的内存草稿。"
                "请稍后继续修复。"
            )
        else:
            message = (
                f"第 {accepted_layers + 1} 层连续 {failure.get('attempts', 2)} 次没有取得进展"
                f"{f'（节点 {node_id}）' if node_id else ''}；已暂停并保留最后通过的内存草稿。"
                "请补充修复要求后继续。"
            )
        generation.emit("generation.stalled", {
            "reason": reason,
            "message": message,
            "layer": accepted_layers + 1,
            "iteration": iteration,
            "node_id": node_id,
            "attempts": failure.get("attempts", 2),
            "last_failure": generation.last_failure,
            "workflow": generation.draft,
        })
        raise _GenerationStalled(message)

    @staticmethod
    def _record_model_timeout(
        generation: Generation,
        current: WorkflowSpec,
        error: _OpenCodeTimeoutError,
        *,
        accepted_layers: int,
        iteration: int,
        node_id: str | None,
    ) -> dict[str, Any]:
        attempts = json.loads(json.dumps(error.timeout_attempts or [error.evidence()], ensure_ascii=False))
        failure = {
            "phase": "model_timeout",
            "reason": "model_timeout",
            "message": str(error),
            "layer": accepted_layers + 1,
            "iteration": iteration,
            "node_id": node_id,
            "attempts": len(attempts),
            "timeout_seconds": error.timeout_seconds,
            "call_ids": [item["call_id"] for item in attempts],
            "timeout_attempts": attempts,
            "last_activity": attempts[-1]["activity"],
            "candidate_accepted": False,
        }
        generation.emit("generation.layer_failed", failure)
        generation.emit("workflow.preview", {
            "workflow": current.model_dump(mode="json"),
            "layer": accepted_layers,
            "reverted": True,
            "reason": "model_timeout",
        })
        generation.emit("generation.repairing", {
            "phase": "model_timeout",
            "node_id": node_id,
            "attempt": len(attempts) + 1,
            "attempts": len(attempts) + 1,
            "timeout_attempts": len(attempts),
            "message": "模型调用超时，正在继续当前生成循环",
            "strategy": "continue_generation",
        })
        return failure

    @staticmethod
    def _evaluate_failed_cases_first(
        generation: Generation,
        evaluator: WorkflowEvaluator,
        project: Any,
        candidate: WorkflowSpec,
        iteration: int,
        failed_case_ids: set[str],
    ) -> tuple[CandidateResult, set[str]]:
        def emit_case(info: dict[str, Any]) -> None:
            generation.emit("generation.stage", {
                "stage": "evaluating_case",
                "iteration": iteration,
                "case_index": info.get("index"),
                "case_total": info.get("total"),
                "case_id": info.get("case_id"),
                "case_name": info.get("name"),
                "case_phase": info.get("phase"),
                "case_passed": info.get("passed"),
                "case_duration_seconds": info.get("duration_seconds"),
            })
        if failed_case_ids:
            generation.emit("generation.stage", {
                "stage": "retrying_failed_cases",
                "iteration": iteration,
                "case_ids": sorted(failed_case_ids),
            })
            focused = evaluator.evaluate(
                project, candidate, iteration,
                case_ids=set(failed_case_ids),
                **({"on_case": emit_case} if "on_case" in inspect.signature(evaluator.evaluate).parameters else {}),
            )
            if not focused.passed:
                return focused, {
                    case.case_id for case in focused.cases if not case.passed
                } or set(failed_case_ids)
            generation.emit("generation.stage", {
                "stage": "final_regression",
                "iteration": iteration,
            })
        result = evaluator.evaluate(
            project, candidate, iteration,
            **({"on_case": emit_case} if "on_case" in inspect.signature(evaluator.evaluate).parameters else {}),
        )
        return result, {case.case_id for case in result.cases if not case.passed}

    def _build_creation(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        current = original.model_copy(deep=True)
        failures = [json.loads(json.dumps(item, ensure_ascii=False)) for item in generation.initial_failures]
        accepted_layers = 0
        iteration = 0
        evaluation_locked = False
        failed_case_ids: set[str] = set()
        progress = _BuildProgressGuard.from_workflow(current)
        max_iterations = _incremental_max_iterations()
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            iteration += 1
            if max_iterations and iteration > max_iterations:
                raise RuntimeError(f"创建达到运维配置的最大迭代次数 {max_iterations}，新工作流未保存")
            generation.emit("generation.stage", {
                "stage": "planning_creation_layer", "operation": "create_workflow",
                "validation_tier": "step_schema", "layer": accepted_layers + 1, "iteration": iteration,
            })
            latest_failure = failures[-1] if failures else {}
            try:
                raw = self._invoke_result(
                    generation, spec, command, workdir,
                    CREATION_STEP_PROMPT.format(
                        catalog=catalog_json,
                        request=generation.prompt,
                        workflow=json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
                        feedback=json.dumps(failures[-5:], ensure_ascii=False),
                        layer=accepted_layers,
                    ),
                    f"创建工作流第 {accepted_layers + 1} 层（迭代 {iteration}）",
                    timeout_seconds=_repair_timeout_seconds() if failures else _planning_timeout_seconds(),
                    retry_silent_timeout=bool(failures),
                    retry_node_id=latest_failure.get("node_id"),
                )
            except _OpenCodeTimeoutError as exc:
                failures.append(self._record_model_timeout(
                    generation, current, exc,
                    accepted_layers=accepted_layers, iteration=iteration,
                    node_id=latest_failure.get("node_id"),
                ))
                continue
            try:
                candidate, action, touched_node_id, summary = _apply_creation_step(
                    current, raw, generation.harness_agent_ids,
                )
                static_errors = _creation_step_errors(current, candidate, action, touched_node_id)
                if action != "finish_creation":
                    static_errors.extend(validate_executable_workflow(
                        _project_with_workflow(spec, candidate), candidate,
                        runtime=True, require_output=False,
                    ))
                    static_errors = list(dict.fromkeys(static_errors))
                if static_errors:
                    raise RuntimeError("；".join(static_errors))
            except (ValidationError, ValueError, RuntimeError) as exc:
                failure = {
                    "phase": "creation_step", "message": str(exc), "iteration": iteration,
                    "action": raw.get("action") if isinstance(raw, dict) else None,
                    "candidate_accepted": False,
                }
                failure["node_id"] = _proposal_node_id(raw)
                failure, stalled = progress.record_failure(current, raw, failure)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"), "layer": accepted_layers,
                    "reverted": True, "reason": "creation_step",
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue
            generation.emit("workflow.preview", {
                "workflow": candidate.model_dump(mode="json"), "layer": accepted_layers + 1,
                "operation": "create_workflow", "validation_tier": "local_graph",
                "action": action, "node_id": touched_node_id, "summary": summary,
            })
            if action != "finish_creation":
                cycle_kind = progress.cycle_kind(candidate)
                if cycle_kind == "unchanged":
                    failure = {
                        "phase": "progress_unchanged",
                        "reason": "same_graph",
                        "message": "候选只改变位置、摘要或其他非语义字段，稳定图没有变化",
                        "iteration": iteration, "action": action, "node_id": touched_node_id,
                        "candidate_accepted": False,
                    }
                    failure, stalled = progress.record_failure(current, raw, failure, candidate)
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    generation.emit("workflow.preview", {
                        "workflow": current.model_dump(mode="json"), "layer": accepted_layers,
                        "reverted": True, "reason": "progress_unchanged",
                    })
                    if stalled:
                        self._stall(
                            generation, current, failure,
                            accepted_layers=accepted_layers, iteration=iteration,
                        )
                    continue
                if cycle_kind == "history":
                    failure = {
                        "phase": "progress_cycle", "message": "候选会回到已接受过的工作流状态",
                        "reason": "accepted_graph_cycle",
                        "iteration": iteration, "action": action, "node_id": touched_node_id,
                        "candidate_accepted": False, "attempts": 2,
                    }
                    generation.emit("generation.layer_failed", failure)
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                validation_tier = "static_only"
                if _step_requires_runtime_probe(current, candidate, action, touched_node_id):
                    generation.emit("generation.stage", {
                        "stage": "probing_layer", "operation": "create_workflow",
                        "layer": accepted_layers + 1, "node_id": touched_node_id,
                        "iteration": iteration, "validation_tier": "runtime_probe",
                    })
                    probe_errors = self._probe_incremental_workflow(
                        generation, spec, candidate, touched_node_id,
                        raw.get("probe_input") if isinstance(raw, dict) else None,
                        raw.get("probe_approvals", {}) if isinstance(raw, dict) and isinstance(raw.get("probe_approvals", {}), dict) else {},
                        evaluator,
                    )
                    if probe_errors:
                        failure = {
                            "phase": "runtime_probe", "errors": probe_errors,
                            "iteration": iteration, "action": action, "node_id": touched_node_id,
                            "candidate_accepted": False,
                        }
                        failure, stalled = progress.record_failure(current, raw, failure, candidate)
                        failures.append(failure)
                        generation.emit("generation.layer_failed", failure)
                        generation.emit("workflow.preview", {
                            "workflow": current.model_dump(mode="json"), "layer": accepted_layers,
                            "reverted": True, "reason": "runtime_probe",
                        })
                        if stalled:
                            self._stall(
                                generation, current, failure,
                                accepted_layers=accepted_layers, iteration=iteration,
                            )
                        continue
                    validation_tier = "runtime_probe"
                else:
                    generation.emit("generation.stage", {
                        "stage": "static_layer_accepted", "operation": "create_workflow",
                        "layer": accepted_layers + 1, "node_id": touched_node_id,
                        "iteration": iteration, "validation_tier": "static_only",
                    })
                if not progress.accept(candidate):
                    raise RuntimeError("内部进展状态不一致：候选图已被接受")
                current = candidate
                generation.draft = current.model_dump(mode="json")
                accepted_layers += 1
                failures.clear()
                generation.emit("generation.layer_completed", {
                    "layer": accepted_layers, "operation": "create_workflow",
                    "validation_tier": validation_tier, "action": action,
                    "node_id": touched_node_id, "summary": summary,
                    "nodes": len(current.nodes), "edges": len(current.edges),
                })
                continue

            generation.emit("generation.stage", {
                "stage": "validating_complete_graph", "operation": "create_workflow",
                "validation_tier": "full_static_graph", "iteration": iteration,
            })
            project = _project_with_workflow(spec, candidate)
            full_errors = validate_executable_workflow(project, candidate, runtime=True, require_output=True)
            if full_errors:
                failure = {
                    "phase": "full_static_graph", "errors": full_errors, "iteration": iteration,
                    "action": action, "candidate_accepted": False,
                }
                failure, stalled = progress.record_failure(current, raw, failure, candidate)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue
            if spec.harness:
                generation.emit("generation.stage", {
                    "stage": "checking_runtime", "operation": "create_workflow",
                    "validation_tier": "runtime_readiness",
                })
                evaluator.ensure_harness_ready({item.backend_id for item in spec.harness})
            if not evaluation_locked:
                generation.emit("generation.stage", {
                    "stage": "preparing_cases", "operation": "create_workflow",
                    "validation_tier": "full_runtime_evaluation",
                })
                case_prompt = (
                    f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                    f"\n当前完整工作流：{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}"
                    f"\n本轮目标：{generation.prompt}"
                )
                candidate.evaluation = self._generate_evaluation(
                    generation, spec, command, workdir, case_prompt, original.evaluation,
                )
                current.evaluation = candidate.evaluation.model_copy(deep=True)
                evaluation_locked = True
            else:
                candidate.evaluation = current.evaluation.model_copy(deep=True)
            generation.emit("generation.stage", {
                "stage": "full_evaluating", "operation": "create_workflow",
                "validation_tier": "full_runtime_evaluation", "iteration": iteration,
            })
            result, failed_case_ids = self._evaluate_failed_cases_first(
                generation, evaluator, _project_with_workflow(spec, candidate),
                candidate, iteration, failed_case_ids,
            )
            if result.passed:
                generation.emit("generation.workflow_verified", {
                    "operation": "create_workflow", "validation_tier": "full_runtime_evaluation",
                    "layers": accepted_layers, "iterations": iteration,
                })
                return candidate
            failure = {
                "phase": "full_evaluation", "feedback": self._result_feedback(result),
                "iteration": iteration, "action": action, "candidate_accepted": False,
            }
            failure, stalled = progress.record_failure(current, raw, failure, candidate)
            failures.append(failure)
            generation.emit("generation.layer_failed", failure)
            generation.emit("workflow.preview", {
                "workflow": current.model_dump(mode="json"), "layer": accepted_layers,
                "reverted": True, "reason": "full_evaluation",
            })
            if stalled:
                self._stall(
                    generation, current, failure,
                    accepted_layers=accepted_layers, iteration=iteration,
                )

    def _build_direct(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        current = original.model_copy(deep=True)
        failures: list[dict[str, Any]] = []
        evaluation_locked = False
        failed_case_ids: set[str] = set()
        max_attempts = _direct_max_attempts()
        node_types = ", ".join(sorted(WORKFLOW_NODE_TYPES))
        attempt = 0
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            attempt += 1
            if attempt > max_attempts:
                raise RuntimeError(f"直出生成达到最大尝试次数 {max_attempts}，工作流未保存")
            generation.emit("generation.stage", {
                "stage": "direct_generation", "operation": "create_workflow",
                "validation_tier": "full_graph", "attempt": attempt,
            })
            prompt = DIRECT_CREATION_PROMPT.format(
                node_types=node_types,
                catalog=catalog_json,
                request=generation.prompt,
                workflow=json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
                feedback=json.dumps(failures[-5:], ensure_ascii=False),
            )
            try:
                raw = self._invoke_result(
                    generation, spec, command, workdir, prompt,
                    f"直出生成工作流（尝试 {attempt}）",
                    timeout_seconds=_repair_timeout_seconds() if failures else _planning_timeout_seconds(),
                    retry_silent_timeout=bool(failures),
                )
            except _OpenCodeTimeoutError as exc:
                failures.append(self._record_model_timeout(
                    generation, current, exc,
                    accepted_layers=attempt, iteration=attempt, node_id=None,
                ))
                continue
            # 规范化 + 静态校验
            try:
                normalized = _normalize_workflow_result(
                    raw, generation.harness_agent_ids,
                    expected_workflow_id=generation.workflow_id,
                )
                candidate = WorkflowSpec.model_validate(normalized)
                project = _project_with_workflow(spec, candidate)
                static_errors = validate_executable_workflow(project, candidate, runtime=True, require_output=True)
                if static_errors:
                    raise RuntimeError("；".join(static_errors))
            except (ValidationError, ValueError, RuntimeError) as exc:
                failure = {
                    "phase": "direct_static", "message": str(exc),
                    "attempt": attempt, "candidate_accepted": False,
                }
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "attempt": attempt, "reverted": True, "reason": "direct_static",
                })
                continue
            generation.emit("workflow.preview", {
                "workflow": candidate.model_dump(mode="json"),
                "attempt": attempt, "operation": "create_workflow",
                "validation_tier": "local_graph",
            })
            # 运行时验收
            if spec.harness:
                generation.emit("generation.stage", {
                    "stage": "checking_runtime", "operation": "create_workflow",
                    "validation_tier": "runtime_readiness",
                })
                evaluator.ensure_harness_ready({item.backend_id for item in spec.harness})
            if not evaluation_locked:
                generation.emit("generation.stage", {
                    "stage": "preparing_cases", "operation": "create_workflow",
                    "validation_tier": "full_runtime_evaluation",
                })
                case_prompt = (
                    f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                    f"\n当前完整工作流：{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}"
                    f"\n本轮目标：{generation.prompt}"
                )
                candidate.evaluation = self._generate_evaluation(
                    generation, spec, command, workdir, case_prompt, original.evaluation,
                )
                current.evaluation = candidate.evaluation.model_copy(deep=True)
                evaluation_locked = True
            else:
                candidate.evaluation = current.evaluation.model_copy(deep=True)
            generation.emit("generation.stage", {
                "stage": "full_evaluating", "operation": "create_workflow",
                "validation_tier": "full_runtime_evaluation", "attempt": attempt,
            })
            result, failed_case_ids = self._evaluate_failed_cases_first(
                generation, evaluator, _project_with_workflow(spec, candidate),
                candidate, attempt, failed_case_ids,
            )
            if result.passed:
                generation.emit("generation.workflow_verified", {
                    "operation": "create_workflow", "validation_tier": "full_runtime_evaluation",
                    "attempts": attempt,
                })
                return candidate
            failure = {
                "phase": "full_evaluation", "feedback": self._result_feedback(result),
                "attempt": attempt, "candidate_accepted": False,
            }
            failures.append(failure)
            generation.emit("generation.layer_failed", failure)
            generation.emit("workflow.preview", {
                "workflow": current.model_dump(mode="json"),
                "attempt": attempt, "reverted": True, "reason": "full_evaluation",
            })

    def _build_agent_loop(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        """LLM Agent Loop（DAG 工具宿主，兼容旧 toolcalls 协议）。

        与 _build_incrementally 的区别：模型一次响应可以输出**多个**图操作
        （有序数组），服务端逐个校验、逐个应用、逐个回填结果，模型据此自主
        决定下一步，而不是「每次响应只能一个操作」。控制权从服务端硬编码
        转移到模型，服务端只做确定性校验与运行时探测兜底；agent_loop
        还允许模型显式选择 evaluate、finalize 和 ask_user。
        """
        current = original.model_copy(deep=True)
        failures = [json.loads(json.dumps(item, ensure_ascii=False)) for item in generation.initial_failures]
        accepted_layers = 0
        evaluation_locked = False
        evaluation_passed = False
        evaluation_fingerprint: str | None = None
        failed_case_ids: set[str] = set()
        progress = _BuildProgressGuard.from_workflow(current)
        max_iterations = (
            _agent_loop_max_iterations()
            if generation.build_mode == "agent_loop"
            else _incremental_max_iterations()
        )
        iteration = 0
        chains_text = command_chain_catalog_text()
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            iteration += 1
            if max_iterations and iteration > max_iterations:
                raise RuntimeError(f"工具调用构建达到最大迭代次数 {max_iterations}，原工作流未改变")
            generation.emit("generation.stage", {
                "stage": "agent_loop_turn" if generation.build_mode == "agent_loop" else "toolcalls_planning",
                "iteration": iteration,
                "accepted_layers": accepted_layers,
            })
            prompt = AGENT_LOOP_PROMPT.format(
                chains=chains_text,
                catalog=catalog_json,
                request=generation.prompt,
                workflow=json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
                feedback=json.dumps(failures[-5:], ensure_ascii=False),
            )
            latest_failure = failures[-1] if failures else {}
            try:
                raw = self._invoke_result(
                    generation, spec, command, workdir, prompt,
                    f"工具调用构建（迭代 {iteration}）",
                    timeout_seconds=_repair_timeout_seconds() if failures else _planning_timeout_seconds(),
                    retry_silent_timeout=bool(failures),
                    retry_node_id=latest_failure.get("node_id"),
                )
            except _OpenCodeTimeoutError as exc:
                failures.append(self._record_model_timeout(
                    generation, current, exc,
                    accepted_layers=accepted_layers, iteration=iteration,
                    node_id=latest_failure.get("node_id"),
                ))
                continue
            try:
                actions = _coerce_action_list(raw)
            except RuntimeError as exc:
                failure = {"phase": "toolcalls_contract", "message": str(exc), "iteration": iteration, "candidate_accepted": False}
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                continue

            halted = False
            for action in actions:
                if generation.cancelled:
                    raise RuntimeError("生成已取消")
                raw_action = str(action.get("action", "")).strip()
                if raw_action == "ask_user":
                    question = str(action.get("question") or action.get("message") or "").strip()
                    if not question:
                        failure = {
                            "phase": "agent_loop_contract", "message": "ask_user 缺少 question",
                            "iteration": iteration, "candidate_accepted": False,
                        }
                        failures.append(failure)
                        generation.emit("generation.layer_failed", failure)
                        halted = True
                        break
                    generation.emit("generation.agent_tool", {
                        "action": raw_action, "accepted": True,
                        "question": question, "options": action.get("options", []),
                        "iteration": iteration,
                    })
                    self._await_user(generation, current, question, action.get("options", []), iteration)

                # LLM 显式请求真实验收；complete 保留旧协议的隐式 finalize 语义。
                if raw_action in {"evaluate", "complete"}:
                    if not evaluation_locked:
                        generation.emit("generation.stage", {"stage": "preparing_cases"})
                        case_prompt = (
                            f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                            f"\n当前完整工作流：{json.dumps(current.model_dump(mode='json'), ensure_ascii=False)}"
                            f"\n本轮目标：{generation.prompt}"
                        )
                        current.evaluation = self._generate_evaluation(
                            generation, spec, command, workdir, case_prompt, original.evaluation,
                        )
                        evaluation_locked = True
                    generation.emit("generation.stage", {
                        "stage": "full_evaluating", "iteration": iteration,
                        "validation_tier": "full_runtime_evaluation",
                    })
                    project = _project_with_workflow(spec, current)
                    result, failed_case_ids = self._evaluate_failed_cases_first(
                        generation, evaluator, project, current, iteration, failed_case_ids,
                    )
                    evaluation_passed = bool(result.passed)
                    evaluation_fingerprint = _workflow_semantic_fingerprint(current)
                    generation.emit("generation.agent_tool", {
                        "action": raw_action, "accepted": evaluation_passed,
                        "passed": evaluation_passed, "iteration": iteration,
                        "feedback": self._result_feedback(result),
                    })
                    if result.passed:
                        if raw_action == "complete":
                            generation.emit("generation.workflow_verified", {
                                "validation_tier": "full_runtime_evaluation",
                                "layers": accepted_layers, "iterations": iteration,
                            })
                            return current
                        continue
                    failure = {
                        "phase": "full_evaluation", "feedback": self._result_feedback(result),
                        "iteration": iteration, "action": "complete", "candidate_accepted": False,
                    }
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    generation.emit("workflow.preview", {
                        "workflow": current.model_dump(mode="json"),
                        "reverted": True, "reason": "full_evaluation",
                    })
                    halted = True
                    break
                if raw_action == "finalize":
                    current_fingerprint = _workflow_semantic_fingerprint(current)
                    if not evaluation_passed or current_fingerprint != evaluation_fingerprint:
                        failure = {
                            "phase": "agent_loop_finalize",
                            "message": "finalize 前必须先让当前未变更 DAG 通过 evaluate",
                            "iteration": iteration, "candidate_accepted": False,
                        }
                        failures.append(failure)
                        generation.emit("generation.agent_tool", {
                            "action": raw_action, "accepted": False,
                            "iteration": iteration, "message": failure["message"],
                        })
                        generation.emit("generation.layer_failed", failure)
                        halted = True
                        break
                    generation.emit("generation.agent_tool", {
                        "action": raw_action, "accepted": True, "iteration": iteration,
                    })
                    generation.emit("generation.workflow_verified", {
                        "validation_tier": "full_runtime_evaluation",
                        "layers": accepted_layers, "iterations": iteration,
                    })
                    return current
                # 普通操作：逐个校验、应用、探测
                try:
                    candidate, act, touched, probe_input, probe_approvals, summary = _apply_incremental_step(
                        current, action, generation.harness_agent_ids,
                        allow_delete=_explicit_delete_request(generation.prompt) and not generation.optimize_only and not failures,
                    )
                except (ValidationError, ValueError, RuntimeError) as exc:
                    failure = {
                        "phase": "toolcalls_step", "message": str(exc), "iteration": iteration,
                        "action": raw_action, "node_id": _proposal_node_id(action), "candidate_accepted": False,
                    }
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    halted = True
                    break
                static_errors = _incremental_connectivity_errors(
                    spec, current, candidate, act, touched, require_output=False,
                )
                dag_errors = _dag_errors(candidate)
                static_errors = list(dict.fromkeys([*dag_errors, *static_errors]))
                if static_errors and (act != "delete_node" or dag_errors):
                    failure = {
                        "phase": "dag" if dag_errors else "connectivity", "errors": static_errors, "action": act,
                        "node_id": touched, "iteration": iteration, "candidate_accepted": False,
                    }
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    halted = True
                    break
                cycle_kind = progress.cycle_kind(candidate)
                if cycle_kind in {"unchanged", "history"}:
                    failure = {
                        "phase": "progress_cycle" if cycle_kind == "history" else "progress_unchanged",
                        "reason": "accepted_graph_cycle" if cycle_kind == "history" else "same_graph",
                        "message": "候选没有产生语义变化或回到已接受状态",
                        "iteration": iteration, "action": act, "node_id": touched, "candidate_accepted": False,
                    }
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    halted = True
                    break
                probe_errors: list[str] = []
                if _step_requires_runtime_probe(current, candidate, act, touched):
                    generation.emit("generation.stage", {
                        "stage": "probing_layer", "iteration": iteration,
                        "node_id": touched, "validation_tier": "runtime_probe",
                    })
                    probe_errors = self._probe_incremental_workflow(
                        generation, spec, candidate,
                        None if act == "delete_node" else _probe_anchor(touched),
                        probe_input, probe_approvals, evaluator,
                    )
                if probe_errors:
                    failure = {
                        "phase": "runtime_probe", "errors": probe_errors, "action": act,
                        "node_id": touched, "iteration": iteration, "candidate_accepted": False,
                    }
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    halted = True
                    break
                if not progress.accept(candidate):
                    raise RuntimeError("内部进展状态不一致：候选图已被接受")
                current = candidate
                evaluation_passed = False
                evaluation_fingerprint = None
                generation.draft = current.model_dump(mode="json")
                accepted_layers += 1
                generation.emit("generation.layer_completed", {
                    "layer": accepted_layers, "action": act, "node_id": touched,
                    "summary": summary, "nodes": len(current.nodes), "edges": len(current.edges),
                })
                generation.emit("workflow.updated", {
                    "workflow": current.model_dump(mode="json"), "layer": accepted_layers,
                    "action": act, "node_id": touched,
                })
            if halted:
                failures = failures[-5:]
                continue

    def _build_toolcalls(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        """Compatibility wrapper for legacy toolcalls mode."""
        return self._build_agent_loop(
            generation, spec, command, workdir, original, evaluator, catalog_json,
        )

    def _build_incrementally(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        original: WorkflowSpec,
        evaluator: WorkflowEvaluator,
        catalog_json: str,
    ) -> WorkflowSpec:
        current = original.model_copy(deep=True)
        failures = [json.loads(json.dumps(item, ensure_ascii=False)) for item in generation.initial_failures]
        accepted_layers = 0
        iteration = 0
        evaluation_locked = False
        failed_case_ids: set[str] = set()
        progress = _BuildProgressGuard.from_workflow(current)
        max_iterations = _incremental_max_iterations()
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            iteration += 1
            if max_iterations and iteration > max_iterations:
                raise RuntimeError(
                    f"增量构建达到运维配置的最大迭代次数 {max_iterations}，原工作流未改变"
                )
            generation.emit("generation.stage", {
                "stage": "planning_layer", "layer": accepted_layers + 1, "iteration": iteration,
            })
            prompt = INCREMENTAL_STEP_PROMPT.format(
                catalog=catalog_json,
                chains=command_chain_catalog_text(),
                request=generation.prompt,
                workflow=json.dumps(current.model_dump(mode="json"), ensure_ascii=False),
                feedback=json.dumps(failures[-5:], ensure_ascii=False),
                layer=accepted_layers,
            )
            latest_failure = failures[-1] if failures else {}
            repairing_failed_delete = (
                latest_failure.get("action") == "delete_node"
                and latest_failure.get("phase") in {"structural_runtime", "runtime_probe"}
            )
            if failures and (not _explicit_delete_request(generation.prompt) or repairing_failed_delete):
                failed_node = str(latest_failure.get("node_id") or "")
                candidate_was_accepted = bool(latest_failure.get("candidate_accepted"))
                if latest_failure.get("action") == "add_node" and not candidate_was_accepted and failed_node not in {node.id for node in current.nodes}:
                    repair_rule = f"该 add_node 候选已回滚，节点 {failed_node or '<unknown>'} 不在当前图中；必须使用 add_node 以相同合法 id 重新创建它。"
                elif repairing_failed_delete:
                    repair_rule = "删除候选已整体测试并回滚；必须使用 add_node 或 update_node 修复剩余图。"
                else:
                    repair_rule = "失败节点已存在于当前 accepted graph；必须使用 update_node 修复其参数、prompt、输入映射或连线。"
                prompt += f"\n本轮处于失败修复阶段。请先阅读最近失败证据并明确诊断原因；{repair_rule}禁止返回 delete_node，也不要用删除节点来规避运行错误。"
            try:
                raw = self._invoke_result(
                    generation, spec, command, workdir, prompt,
                    f"增量构建第 {accepted_layers + 1} 层（迭代 {iteration}）",
                    timeout_seconds=_repair_timeout_seconds() if failures else _planning_timeout_seconds(),
                    retry_silent_timeout=bool(failures),
                    retry_node_id=latest_failure.get("node_id"),
                )
            except _OpenCodeTimeoutError as exc:
                failures.append(self._record_model_timeout(
                    generation, current, exc,
                    accepted_layers=accepted_layers, iteration=iteration,
                    node_id=latest_failure.get("node_id"),
                ))
                continue
            raw_action = raw.get("action") if isinstance(raw, dict) else None
            if generation.optimize_only and raw_action == "delete_node":
                failure = {
                    "phase": "operation_policy",
                    "message": "优化模式禁止删除节点；请使用 update_node 保持目标效果并优化现有图",
                    "node_id": raw.get("node_id") or raw.get("id"),
                    "iteration": iteration,
                    "action": raw_action,
                    "candidate_accepted": False,
                }
                failure["node_id"] = failure.get("node_id") or _proposal_node_id(raw)
                failure, stalled = progress.record_failure(current, raw, failure)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue
            if failures and raw_action == "delete_node" and not _explicit_delete_request(generation.prompt):
                failure = {
                    "phase": "repair_policy",
                    "message": "运行失败后的修复阶段禁止删除节点；必须根据 accepted graph 判断使用 add_node 重建或 update_node 修复",
                    "node_id": raw.get("node_id") or raw.get("id"),
                    "iteration": iteration,
                    "action": raw_action,
                    "candidate_accepted": False,
                }
                failure["node_id"] = failure.get("node_id") or _proposal_node_id(raw)
                failure, stalled = progress.record_failure(current, raw, failure)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": "repair_policy",
                    "node_id": failure["node_id"],
                    "message": failure["message"],
                    "strategy": "recreate_or_update_accepted_node",
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue
            try:
                candidate, action, touched_node_id, probe_input, probe_approvals, summary = _apply_incremental_step(
                    current, raw, generation.harness_agent_ids,
                    allow_delete=_explicit_delete_request(generation.prompt) and not generation.optimize_only and not failures,
                )
            except (ValidationError, ValueError, RuntimeError) as exc:
                failure = {
                    "phase": "step_contract", "message": str(exc), "iteration": iteration,
                    "action": raw_action, "node_id": _proposal_node_id(raw),
                    "candidate_accepted": False,
                }
                failure, stalled = progress.record_failure(current, raw, failure)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": failure.get("message") or "已收集失败证据，下一轮将诊断并修复原节点",
                    "strategy": "recreate_or_update_accepted_node",
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue

            # Expose the model's validated incremental result immediately so
            # the Studio canvas can render the new node/parameters while the
            # runtime probe is still running. A later preview event restores
            # the last accepted graph if the probe rejects this candidate.
            generation.emit("workflow.preview", {
                "workflow": candidate.model_dump(mode="json"),
                "layer": accepted_layers + 1,
                "action": action,
                "node_id": touched_node_id,
                "summary": summary,
            })

            generation.emit("generation.step_proposed", {
                "action": action, "node_id": touched_node_id, "summary": summary,
                "layer": accepted_layers + 1, "iteration": iteration,
            })
            generation.emit("generation.stage", {
                "stage": "validating_node", "layer": accepted_layers + 1,
                "node_id": touched_node_id, "iteration": iteration,
            })
            static_errors = _incremental_connectivity_errors(
                spec, current, candidate, action, touched_node_id,
                require_output=action == "complete",
            )
            if static_errors and action != "delete_node":
                failure = {
                    "phase": "connectivity", "errors": static_errors, "action": action,
                    "node_id": touched_node_id, "iteration": iteration, "candidate_accepted": False,
                }
                failure, stalled = progress.record_failure(current, raw, failure, candidate)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "结构检查未通过，下一轮将根据错误修复节点或连线",
                    "strategy": "recreate_or_update_accepted_node",
                })
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": "connectivity",
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue

            if action != "complete":
                cycle_kind = progress.cycle_kind(candidate)
                if cycle_kind == "unchanged":
                    failure = {
                        "phase": "progress_unchanged",
                        "reason": "same_graph",
                        "message": "候选只改变位置、摘要或其他非语义字段，稳定图没有变化",
                        "iteration": iteration, "action": action, "node_id": touched_node_id,
                        "candidate_accepted": False,
                    }
                    failure, stalled = progress.record_failure(current, raw, failure, candidate)
                    failures.append(failure)
                    generation.emit("generation.layer_failed", failure)
                    generation.emit("generation.repairing", {
                        "phase": failure["phase"], "node_id": touched_node_id,
                        "message": "本轮没有产生语义变化，下一轮必须修改节点参数或连线",
                        "strategy": "semantic_change_required",
                    })
                    generation.emit("workflow.preview", {
                        "workflow": current.model_dump(mode="json"),
                        "layer": accepted_layers, "reverted": True,
                        "reason": "progress_unchanged",
                    })
                    if stalled:
                        self._stall(
                            generation, current, failure,
                            accepted_layers=accepted_layers, iteration=iteration,
                        )
                    continue
                if cycle_kind == "history":
                    failure = {
                        "phase": "progress_cycle", "reason": "accepted_graph_cycle",
                        "message": "候选会回到已接受过的工作流状态",
                        "iteration": iteration, "action": action, "node_id": touched_node_id,
                        "candidate_accepted": False, "attempts": 2,
                    }
                    generation.emit("generation.layer_failed", failure)
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )

            if action == "complete":
                if not evaluation_locked:
                    generation.emit("generation.stage", {"stage": "preparing_cases"})
                    case_prompt = (
                        f"{CASE_PROMPT}\n已有验收用例：{json.dumps(original.evaluation.model_dump(mode='json'), ensure_ascii=False)}"
                        f"\n当前完整工作流：{json.dumps(candidate.model_dump(mode='json'), ensure_ascii=False)}"
                        f"\n本轮目标：{generation.prompt}"
                    )
                    candidate.evaluation = self._generate_evaluation(
                        generation, spec, command, workdir, case_prompt, original.evaluation,
                    )
                    current.evaluation = candidate.evaluation.model_copy(deep=True)
                    evaluation_locked = True
                else:
                    candidate.evaluation = current.evaluation.model_copy(deep=True)
                generation.emit("generation.stage", {
                    "stage": "full_evaluating", "layer": accepted_layers, "iteration": iteration,
                })
                project = spec.model_copy(deep=True)
                project.workflows = [candidate if item.id == candidate.id else item for item in project.workflows]
                result, failed_case_ids = self._evaluate_failed_cases_first(
                    generation, evaluator, project, candidate, iteration, failed_case_ids,
                )
                if result.passed:
                    generation.emit("generation.workflow_verified", {
                        "layers": accepted_layers, "iterations": iteration,
                    })
                    return candidate
                failure = {
                    "phase": "full_evaluation", "feedback": self._result_feedback(result),
                    "iteration": iteration, "action": action, "node_id": touched_node_id,
                    "candidate_accepted": False,
                }
                failure, stalled = progress.record_failure(current, raw, failure, candidate)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "完整验收未通过，下一轮将保留节点并修复失败原因",
                    "strategy": "recreate_or_update_accepted_node",
                })
                current.evaluation = candidate.evaluation.model_copy(deep=True)
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": "full_evaluation",
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue

            probe_errors: list[str] = []
            if _step_requires_runtime_probe(current, candidate, action, touched_node_id):
                generation.emit("generation.stage", {
                    "stage": "probing_layer", "layer": accepted_layers + 1,
                    "node_id": touched_node_id, "iteration": iteration,
                    "validation_tier": "runtime_probe",
                })
                probe_errors = self._probe_incremental_workflow(
                    generation, spec, candidate,
                    None if action == "delete_node" else touched_node_id,
                    probe_input, probe_approvals,
                    evaluator,
                )
            else:
                generation.emit("generation.stage", {
                    "stage": "static_layer_accepted", "layer": accepted_layers + 1,
                    "node_id": touched_node_id, "iteration": iteration,
                    "validation_tier": "static_only",
                })
            failure_errors = list(dict.fromkeys([*static_errors, *probe_errors])) if action == "delete_node" else probe_errors
            if failure_errors:
                failure_phase = "structural_runtime" if action == "delete_node" else "runtime_probe"
                failure = {
                    "phase": failure_phase, "errors": failure_errors, "action": action,
                    "node_id": touched_node_id, "iteration": iteration, "candidate_accepted": False,
                }
                failure, stalled = progress.record_failure(current, raw, failure, candidate)
                failures.append(failure)
                generation.emit("generation.layer_failed", failure)
                generation.emit("generation.repairing", {
                    "phase": failure["phase"], "node_id": failure.get("node_id"),
                    "message": "删除后的整体结构与运行探测未通过，下一轮将重新连接或重建节点" if action == "delete_node" else "运行探测失败，下一轮将诊断 Harness/参数原因并修复原节点",
                    "strategy": "recreate_or_update_accepted_node",
                })
                generation.emit("workflow.preview", {
                    "workflow": current.model_dump(mode="json"),
                    "layer": accepted_layers,
                    "reverted": True,
                    "reason": failure["phase"],
                })
                if stalled:
                    self._stall(
                        generation, current, failure,
                        accepted_layers=accepted_layers, iteration=iteration,
                    )
                continue
            if not progress.accept(candidate):
                raise RuntimeError("内部进展状态不一致：候选图已被接受")
            current = candidate
            generation.draft = current.model_dump(mode="json")
            accepted_layers += 1
            failures.clear()
            generation.emit("generation.layer_completed", {
                "layer": accepted_layers, "action": action, "node_id": touched_node_id,
                "summary": summary, "nodes": len(current.nodes), "edges": len(current.edges),
            })
            generation.emit("workflow.updated", {
                "workflow": current.model_dump(mode="json"),
                "layer": accepted_layers,
                "action": action,
                "node_id": touched_node_id,
                "summary": summary,
            })

    def _probe_incremental_workflow(
        self,
        generation: Generation,
        spec: Any,
        workflow: WorkflowSpec,
        touched_node_id: str | None,
        probe_input: Any,
        probe_approvals: dict[str, bool],
        evaluator: WorkflowEvaluator,
    ) -> list[str]:
        # Probe only the newly touched layer and the nearest approval/condition
        # path needed to reach it. Re-running previously accepted AI agents on
        # every layer adds latency and turns transient model failures into
        # unrelated downstream failures. Final acceptance still runs full graph.
        probe_target = _probe_anchor(touched_node_id)
        probe_workflow = _incremental_probe_workflow(workflow, probe_target)
        project = _project_with_workflow(spec, probe_workflow)
        approvals = {node.id: True for node in probe_workflow.nodes if node.type == "approval"}
        approvals.update(probe_approvals)
        body: dict[str, Any] = {"input": probe_input}
        trigger_node_id = _incremental_trigger_for_node(probe_workflow, probe_target)
        if trigger_node_id:
            body["_trigger_node_id"] = trigger_node_id
        manager = WorkflowManager(base_url=evaluator.harness_base_url, poll_interval=0.1)
        policy = EvaluationPolicy(
            approvals=approvals,
            model_inference=evaluator.model_inference,
            live_execution=True,
        )
        try:
            try:
                # Incremental layers are intentionally probed before an output
                # node exists. The final `complete` path is still required to
                # contain an output node by the strict connectivity checks.
                run = manager.start(
                    project,
                    probe_workflow.id,
                    body,
                    policy=policy,
                    record=False,
                    require_output=False,
                )
            except (RuntimeError, ValueError) as exc:
                message = str(exc)
                if _is_harness_infrastructure_message(message):
                    raise HarnessInfrastructureError(
                        f"Harness 增量层探测基础设施失败：{message}。当前层未进入无效重建。"
                    ) from exc
                return [message]
            deadline = time.monotonic() + _incremental_probe_timeout_seconds()
            while run.status not in TERMINAL_RUN_STATES and time.monotonic() < deadline:
                if generation.cancelled:
                    run.cancel_event.set()
                    raise RuntimeError("生成已取消")
                time.sleep(0.02)
            if run.status not in TERMINAL_RUN_STATES:
                run.cancel_event.set()
                return [f"第 {touched_node_id or '当前'} 层真实探测超时"]
            if run.status != "completed":
                if run.error_code or _is_harness_infrastructure_message(run.error):
                    raise HarnessInfrastructureError(
                        f"Harness 增量层探测基础设施失败（code={run.error_code or 'unknown'}）：{run.error}。当前层未进入无效重建。"
                    )
                return [run.error or f"增量层状态为 {run.status}"]
            if probe_target and probe_target in run.node_states:
                status = str(run.node_states[probe_target].get("status", ""))
                if status != "completed":
                    return [f"本层节点 {probe_target} 未被真实执行，状态为 {status or 'unknown'}；请调整探测输入或连线"]
            return []
        finally:
            manager.stop_scheduler()

    def _route_chat_turn(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        workflow: WorkflowSpec,
    ) -> dict[str, Any]:
        history = self.history.get(generation.workflow_id, [])[-20:]
        if history and history[-1].get("role") == "user" and history[-1].get("content") == generation.prompt:
            history = history[:-1]
        prompt = (
            f"{CHAT_ROUTER_PROMPT}\n"
            f"当前工作流：{json.dumps(workflow.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"最近对话：{json.dumps(history, ensure_ascii=False)}\n"
            f"用户本轮消息：{generation.prompt}"
        )
        raw = self._invoke_result(generation, spec, command, workdir, prompt, "OpenCode 对话")
        if not isinstance(raw, dict) or raw.get("action") not in {"reply", "modify"}:
            raise RuntimeError("OpenCode 对话结果缺少有效 action（应为 reply 或 modify）")
        action = str(raw["action"])
        field = "answer" if action == "reply" else "request"
        text = str(raw.get(field, "")).strip()
        if not text:
            raise RuntimeError(f"OpenCode 对话结果缺少非空 {field}")
        if action == "reply":
            raw_options = raw.get("options", [])
            options = [str(item).strip() for item in raw_options] if isinstance(raw_options, list) else []
            options = [item for item in options if item and len(item) <= 120]
            if len(options) != 3 or len(set(options)) != 3:
                options = []
            return {"action": action, field: text, "options": options}
        return {"action": action, field: text}

    def _compact_prompt(
        self,
        generation: Generation,
        binary: str,
        model: str,
        context: str,
        workdir: Path,
        environment: dict[str, str],
    ) -> str:
        command = [
            binary, "run", "--pure", "--format", "json", "--agent", os.environ.get("OPENCODE_COMPACTION_AGENT", "openagent-generator"),
            "--title", "OpenAgent内部上下文提炼",
            "--model", model,
        ]
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        process = subprocess.Popen(
            command, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, bufsize=1, env=environment, encoding="utf-8", errors="replace",
        )
        generation.process = process
        assert process.stdout is not None
        assert process.stdin is not None
        process.stdin.write(f"{COMPACTION_PROMPT}\n\n待压缩原始上下文：\n{context}")
        process.stdin.close()
        text = ""
        diagnostics: list[str] = []
        timed_out = threading.Event()

        def stop_timed_out_process() -> None:
            timed_out.set()
            if process.poll() is None:
                _terminate_process_tree(process)

        timer = threading.Timer(_compaction_timeout_seconds(), stop_timed_out_process)
        timer.daemon = True
        timer.start()
        self._write_opencode_log(generation, {
            "call_id": call_id, "purpose": "上下文提炼", "status": "started",
            "pid": process.pid, "timeout_seconds": _compaction_timeout_seconds(), "model": model,
        })
        code: int | None = None
        try:
            for raw in process.stdout:
                if generation.cancelled:
                    _terminate_process_tree(process)
                    raise RuntimeError("生成已取消")
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    if raw.strip():
                        diagnostics.append(raw.strip())
                    continue
                error_text = _extract_error(item)
                if error_text:
                    diagnostics.append(error_text)
                chunk = _extract_text(item)
                if chunk:
                    text = chunk if chunk.startswith(text) else text + chunk
            code = process.wait()
        finally:
            timer.cancel()
            self._write_opencode_log(generation, {
                "call_id": call_id,
                "purpose": "上下文提炼",
                "status": "timeout" if timed_out.is_set() else ("completed" if code == 0 else "failed"),
                "pid": process.pid,
                "exit_code": code,
                "timeout_seconds": _compaction_timeout_seconds(),
                "duration_ms": _elapsed_ms(started),
                "output_chars": len(text),
                "diagnostics": [_redact_log_text(item) for item in diagnostics[-10:]],
                "response_tail": _redact_log_text(text[-4000:]),
            })
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        if timed_out.is_set():
            raise _CompactionTimeoutError(f"OpenCode 上下文提炼超时（{_compaction_timeout_seconds()} 秒）")
        if code != 0:
            detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
            raise RuntimeError(f"OpenCode 内部上下文提炼失败，代码 {code}：{detail}")
        text = text.strip()
        if not text:
            raise _EmptyCompactionError("OpenCode 内部上下文提炼没有返回内容")
        return text

    def _prepare_prompt(
        self,
        generation: Generation,
        spec: Any,
        base_command: list[str],
        workdir: Path,
        prompt: str,
    ) -> str:
        if len(prompt) < _compact_prompt_length() or generation.compaction_disabled:
            return prompt
        generation.emit("generation.context_compacting", {"before_chars": len(prompt)})
        try:
            try:
                compacted = self._compact_prompt(
                    generation, base_command[0], generation.model, prompt, workdir,
                    self._environment(spec),
                ).strip()
            except _EmptyCompactionError:
                generation.emit("generation.context_compaction_retry", {
                    "before_chars": len(prompt), "reason": "empty_output", "attempt": 2,
                })
                strict_context = (
                    "上一次提炼进程成功退出但返回了空内容。本次必须输出非空的中文提炼结果；"
                    "不要只执行内部压缩，不要沉默结束，不要输出工具调用。必须保留原上下文中的全部硬约束。"
                    f"\n\n待提炼的完整原始上下文：\n{prompt}"
                )
                try:
                    compacted = self._compact_prompt(
                        generation, base_command[0], generation.model, strict_context, workdir,
                        self._environment(spec),
                    ).strip()
                except _EmptyCompactionError as exc:
                    raise _EmptyCompactionError(
                        "OpenCode 内部上下文提炼自动严格重试 1 次后仍没有返回内容"
                    ) from exc
        except _CompactionTimeoutError as exc:
            generation.compaction_disabled = True
            generation.emit("generation.context_compaction_failed", {
                "before_chars": len(prompt), "message": str(exc), "fallback": "original",
            })
            return prompt
        use_compacted = bool(compacted) and len(compacted) < len(prompt)
        generation.emit("generation.context_compacted", {
            "before_chars": len(prompt), "after_chars": len(compacted), "used": use_compacted,
        })
        return compacted if use_compacted else prompt

    def _invoke(
        self,
        generation: Generation,
        spec: Any,
        base_command: list[str],
        workdir: Path,
        prompt: str,
        *,
        timeout_seconds: int | None = None,
        purpose: str = "OpenCode 调用",
        call_attempt: int = 1,
        previous_call_id: str | None = None,
    ) -> str:
        if generation.cancelled:
            raise RuntimeError("生成已取消")
        environment = self._environment(spec)
        command_base = [*base_command, "--model", generation.model]
        prompt = self._prepare_prompt(generation, spec, base_command, workdir, prompt)
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        generation.emit("generation.model_call", {
            "phase": "started",
            "purpose": purpose,
            "model": generation.model,
            "attempt": call_attempt,
        })
        try:
            current_process = subprocess.Popen(
                command_base, cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE, bufsize=1, env=environment, encoding="utf-8", errors="replace",
            )
        except Exception as exc:
            self._write_opencode_log(generation, {
                "call_id": call_id, "purpose": purpose, "status": "spawn_failed", "duration_ms": _elapsed_ms(started),
                "error": _redact_log_text(str(exc)),
            })
            raise
        generation.process = current_process
        assert current_process.stdout is not None
        assert current_process.stdin is not None
        current_process.stdin.write(prompt)
        current_process.stdin.close()
        timed_out = threading.Event()

        def stop_timed_out_process() -> None:
            timed_out.set()
            if current_process.poll() is None:
                _terminate_process_tree(current_process)

        call_timeout = timeout_seconds or _invoke_timeout_seconds()
        timer = threading.Timer(call_timeout, stop_timed_out_process)
        timer.daemon = True
        timer.start()
        retry_metadata = {
            "attempt": call_attempt,
            **({"previous_call_id": previous_call_id} if previous_call_id else {}),
        }
        self._write_opencode_log(generation, {
            "call_id": call_id, "purpose": purpose, "status": "started",
            "pid": current_process.pid, "timeout_seconds": call_timeout, "model": generation.model,
            **retry_metadata,
        })
        assistant_text, diagnostics = "", []
        event_counts: dict[str, int] = {}
        tool_counts: dict[str, int] = {}
        protocol_events = 0
        reasoning_chars = 0
        text_events = 0
        tool_events = 0
        last_event_at = started
        last_event_type = "process_started"
        last_tool: str | None = None
        code: int | None = None
        last_progress_emit = 0.0
        try:
            for raw in current_process.stdout:
                if generation.cancelled:
                    return ""
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    if raw.strip():
                        diagnostics.append(raw.strip())
                    continue
                protocol_events += 1
                event_type = str(item.get("type") or "unknown")
                event_counts[event_type] = event_counts.get(event_type, 0) + 1
                last_event_at = time.monotonic()
                last_event_type = event_type
                part = item.get("part") or item.get("properties", {}).get("part")
                if isinstance(part, dict):
                    part_type = str(part.get("type") or "")
                    if part_type:
                        event_counts[part_type] = event_counts.get(part_type, 0) + 1
                    if part_type == "reasoning":
                        reasoning_chars += len(str(part.get("text") or ""))
                    elif part_type == "text":
                        text_events += 1
                    elif part_type == "tool":
                        tool_events += 1
                        last_tool = str(part.get("tool") or part.get("name") or "unknown")
                        tool_counts[last_tool] = tool_counts.get(last_tool, 0) + 1
                if error := _extract_error(item):
                    diagnostics.append(error)
                text = _extract_text(item)
                if text:
                    assistant_text = text if text.startswith(assistant_text) else assistant_text + text
                    now = time.monotonic()
                    if now - last_progress_emit >= 1.0:
                        last_progress_emit = now
                        generation.emit("generation.model_activity", {
                            "purpose": purpose,
                            "model": generation.model,
                            "output_chars": len(assistant_text),
                        })
            code = current_process.wait()
        finally:
            timer.cancel()
            activity_metrics = {
                "output_chars": len(assistant_text),
                "diagnostics": [_redact_log_text(item) for item in diagnostics[-10:]],
                "protocol_events": protocol_events,
                "event_counts": event_counts,
                "tool_counts": tool_counts,
                "reasoning_chars": reasoning_chars,
                "text_events": text_events,
                "tool_events": tool_events,
                "last_event_type": last_event_type,
                "last_tool": last_tool,
                "last_activity_ms": round((last_event_at - started) * 1000),
                "idle_at_exit_ms": round((time.monotonic() - last_event_at) * 1000),
            }
            self._write_opencode_log(generation, {
                "call_id": call_id,
                "purpose": purpose,
                "status": "timeout" if timed_out.is_set() else ("completed" if code == 0 else "failed"),
                "pid": current_process.pid,
                "exit_code": code,
                "timeout_seconds": call_timeout,
                "duration_ms": _elapsed_ms(started),
                "response_tail": _redact_log_text(assistant_text[-4000:]),
                **activity_metrics,
                **retry_metadata,
            })
            generation.emit("generation.model_call", {
                "phase": "finished",
                "purpose": purpose,
                "model": generation.model,
                "attempt": call_attempt,
                "exit_code": code,
                "duration_ms": _elapsed_ms(started),
                "output_tail": _redact_log_text(assistant_text[-1200:]),
                "diagnostics": [_redact_log_text(item) for item in diagnostics[-5:]],
            })
        if timed_out.is_set():
            activity = (
                f"协议事件 {protocol_events} 个，工具调用 {tool_events} 次，"
                f"reasoning {reasoning_chars} 字，最终文本 {len(assistant_text)} 字；"
                f"最后事件 {last_event_type}，最后工具 {last_tool or '无'}，"
                f"最后活动距启动 {round((last_event_at - started) * 1000)} ms，"
                f"超时前空闲 {round((time.monotonic() - last_event_at) * 1000)} ms"
            )
            raise _OpenCodeTimeoutError(
                f"OpenCode 单次调用超时（{call_timeout} 秒）；{activity}。"
                "详见 .openagent-logs/opencode.jsonl",
                call_id=call_id,
                purpose=purpose,
                timeout_seconds=call_timeout,
                activity=activity_metrics,
                previous_call_id=previous_call_id,
            )
        if tool_events:
            raise RuntimeError(
                f"OpenCode 生成器违反无工具契约：检测到 {tool_events} 次工具调用（最后工具 {last_tool or 'unknown'}）；"
                "请使用 openagent-generator，而不是带工具的 Agent。"
            )
        permission_diagnostics = [item for item in diagnostics if "permission requested" in item.lower() or "external_directory" in item.lower()]
        if permission_diagnostics:
            raise RuntimeError("OpenCode 生成器触发了被禁止的权限请求；请检查无工具 Agent 配置")
        if code == 0:
            return assistant_text
        detail = diagnostics[-1] if diagnostics else "没有返回错误详情"
        raise RuntimeError(f"OpenCode 退出，代码 {code}：{detail}")

    def _invoke_result(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        purpose: str,
        *,
        timeout_seconds: int | None = None,
        retry_silent_timeout: bool = False,
        retry_node_id: str | None = None,
    ) -> Any:
        """Parse model result while recovering every model timeout.

        ``retry_silent_timeout`` remains accepted for embedding compatibility;
        timeout activity level no longer changes recovery policy.
        """
        text = self._invoke_with_timeout_recovery(
            generation, spec, command, workdir, prompt, timeout_seconds, purpose,
            retry_node_id=retry_node_id,
        )
        try:
            return _parse_result(text)
        except StructuredResultError:
            retry_prompt = (
                f"{prompt}\n\n你上一次没有返回可解析的结构化 JSON。请重新完成同一任务，只输出一个 "
                "<result>{合法 JSON}</result>，不要使用注释、尾随逗号、单引号或任何额外说明。"
            )
            retry_text = self._invoke_with_timeout_recovery(
                generation, spec, command, workdir, retry_prompt, timeout_seconds, f"{purpose}（严格重试）",
                retry_node_id=retry_node_id,
            )
            try:
                return _parse_result(retry_text)
            except StructuredResultError as exc:
                raise StructuredResultError(f"{purpose}未返回有效的结构化 JSON（已自动严格重试 1 次）") from exc

    def _invoke_with_timeout_recovery(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        timeout_seconds: int | None,
        purpose: str,
        *,
        retry_node_id: str | None = None,
    ) -> str:
        """Retry model timeouts without converting them into user work.

        Timeout is recoverable model/process state. Keep same prompt, model,
        and workflow context; only explicit cancellation or configured retry
        budget ends this loop. Normal process/protocol errors still propagate.
        """
        timeout_count = 0
        timeout_attempts: list[dict[str, Any]] = []
        call_attempt = 1
        previous_call_id: str | None = None
        while True:
            if generation.cancelled:
                raise RuntimeError("生成已取消")
            kwargs: dict[str, Any] = {}
            if call_attempt != 1:
                kwargs["call_attempt"] = call_attempt
            if previous_call_id is not None:
                kwargs["previous_call_id"] = previous_call_id
            try:
                text = self._invoke_for_result(
                    generation, spec, command, workdir, prompt, timeout_seconds, purpose,
                    **kwargs,
                )
                if generation.cancelled:
                    raise RuntimeError("生成已取消")
                return text
            except _OpenCodeTimeoutError as exc:
                if generation.cancelled:
                    raise RuntimeError("生成已取消") from exc
                evidence = exc.timeout_attempts or [exc.evidence()]
                timeout_count += len(evidence)
                timeout_attempts = [*timeout_attempts, *evidence][-20:]
                limit = _model_timeout_retry_limit()
                if limit and timeout_count > limit:
                    exc.timeout_attempts = timeout_attempts
                    raise
                call_attempt = timeout_count + 1
                previous_call_id = exc.call_id
                generation.emit("generation.repairing", {
                    "phase": "model_timeout",
                    "node_id": retry_node_id,
                    "attempt": call_attempt,
                    "attempts": call_attempt,
                    "previous_call_id": exc.call_id,
                    "timeout_attempts": timeout_count,
                    "message": f"{purpose}调用超时，正在自动继续第 {call_attempt} 次尝试",
                    "strategy": "retry_same_model",
                })

    def _invoke_for_result(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        timeout_seconds: int | None,
        purpose: str,
        *,
        call_attempt: int = 1,
        previous_call_id: str | None = None,
    ) -> str:
        """Invoke while remaining compatible with test/integration overrides."""
        try:
            kwargs: dict[str, Any] = {
                "purpose": purpose,
            }
            if timeout_seconds is not None:
                kwargs["timeout_seconds"] = timeout_seconds
            if call_attempt != 1:
                kwargs["call_attempt"] = call_attempt
            if previous_call_id is not None:
                kwargs["previous_call_id"] = previous_call_id
            return self._invoke(generation, spec, command, workdir, prompt, **kwargs)
        except TypeError as exc:
            # Existing embedders may override _invoke with the historical
            # five-argument signature. Preserve that extension point.
            detail = str(exc)
            if not any(
                name in detail
                for name in ("timeout_seconds", "purpose", "call_attempt", "previous_call_id")
            ):
                raise
            return self._invoke(generation, spec, command, workdir, prompt)

    def _write_opencode_log(self, generation: Generation, data: dict[str, Any]) -> None:
        configured = os.environ.get("OPENAGENT_OPENCODE_LOG")
        path = Path(configured).expanduser() if configured else self.store.path.parent / ".openagent-logs" / "opencode.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            record = _redact_log_value({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "generation_id": generation.id,
                "workflow_id": generation.workflow_id,
                **data,
            })
            with _OPENCODE_LOG_LOCK:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Diagnostics must never turn a model failure into a different failure.
            return

    def _generate_evaluation(
        self,
        generation: Generation,
        spec: Any,
        command: list[str],
        workdir: Path,
        prompt: str,
        previous: WorkflowEvaluation,
    ) -> WorkflowEvaluation:
        raw = self._invoke_result(generation, spec, command, workdir, prompt, "验收用例")
        try:
            return self._validate_evaluation_result(previous, raw)
        except (ValidationError, RuntimeError) as exc:
            retry_prompt = (
                f"{prompt}\n\n你上一次返回的验收用例未通过严格校验。"
                f"\n校验错误：{exc}"
                f"\n无效结果：{json.dumps(raw, ensure_ascii=False)}"
                "\n请修复全部错误后重新输出完整 {\"cases\":[...]}。每个用例必须同时包含至少一条 "
                "assertions 确定性断言和至少一条非空 semantic_criteria 语义质量标准；"
                "operator 不是 exists 时必须显式填写 expected，equals 不得省略 expected；"
                "已有用例仍必须逐字段原样保留。只输出 <result>{合法 JSON}</result>。"
            )
            repaired = self._invoke_result(generation, spec, command, workdir, retry_prompt, "验收用例自动修复")
            try:
                return self._validate_evaluation_result(previous, repaired)
            except (ValidationError, RuntimeError) as repaired_exc:
                raise RuntimeError(f"验收用例自动修复后仍不合格：{repaired_exc}") from repaired_exc

    @classmethod
    def _validate_evaluation_result(cls, previous: WorkflowEvaluation, value: Any) -> WorkflowEvaluation:
        evaluation = WorkflowEvaluation.model_validate(_normalize_evaluation_result(value))
        cls._validate_case_update(previous, evaluation)
        return evaluation

    @staticmethod
    def _validate_case_update(previous: WorkflowEvaluation, current: WorkflowEvaluation) -> None:
        old = [item.model_dump(mode="json") for item in previous.cases]
        new = [item.model_dump(mode="json") for item in current.cases]
        if not old and len(new) != 3:
            raise RuntimeError("首次生成必须创建恰好 3 个验收用例")
        if old and (new[:len(old)] != old or len(new) > len(old) + 3):
            raise RuntimeError("OpenCode 试图删除、修改或一次追加超过 3 个既有验收用例")
        ids = [item.id for item in current.cases]
        if len(ids) != len(set(ids)):
            raise RuntimeError("验收用例 id 重复")
        incomplete = []
        for item in current.cases:
            missing = []
            if not item.assertions:
                missing.append("assertions 确定性断言")
            invalid_assertions = [
                f"{assertion.path}:{assertion.operator}"
                for assertion in item.assertions
                if assertion.operator != "exists" and "expected" not in assertion.model_fields_set
            ]
            if invalid_assertions:
                missing.append(
                    "非 exists 断言缺少 expected（" + ", ".join(invalid_assertions) + "）"
                )
            if not item.semantic_criteria or any(not criterion.strip() for criterion in item.semantic_criteria):
                missing.append("semantic_criteria 语义质量标准")
            if missing:
                incomplete.append(f"{item.id} 缺少 {' 和 '.join(missing)}")
        if incomplete:
            raise RuntimeError(f"每个验收用例都必须同时包含确定性断言和语义质量标准：{'；'.join(incomplete)}")

    def _model_inference(self, generation: Generation, spec: Any, command: list[str], workdir: Path, prompt: str, value: Any) -> Any:
        request = f"执行以下工作流 AI 节点。只输出 <result>{{JSON}}</result>。不得修改文件、运行命令、调用工具或访问非模型网络。\n任务：{prompt}\n输入：{json.dumps(value, ensure_ascii=False)}"
        return self._invoke_result(generation, spec, command, workdir, request, "工作流 AI 节点")

    def _semantic_verdict(self, generation: Generation, spec: Any, command: list[str], workdir: Path, workflow: WorkflowSpec, case: EvaluationCase, output: Any) -> SemanticVerdict:
        prompt = (
            "你是独立 OpenCode 验证智能体，不参与工作流生成。必须根据实际试运行输出逐条验证语义标准。"
            "只输出 <result>{\"passed\":布尔值,\"score\":0到100的整数,\"issues\":[\"未通过原因\"]}</result>。"
            "只有所有标准都满足时 passed 才能为 true；不确定一律判定 false。"
            f"\n工作流：{json.dumps(workflow.model_dump(mode='json', exclude={'evaluation'}), ensure_ascii=False)}"
            f"\n验收标准：{json.dumps(case.semantic_criteria, ensure_ascii=False)}\n实际试运行输出：{json.dumps(output, ensure_ascii=False)}"
        )
        result = self._invoke_result(generation, spec, command, workdir, prompt, "独立语义验证")
        if not isinstance(result, dict):
            return SemanticVerdict(False, 0, ["OpenCode 验证结果格式无效"])
        score = max(0, min(int(result.get("score", 0)), 100))
        issues = [str(item) for item in result.get("issues", [])] if isinstance(result.get("issues", []), list) else []
        return SemanticVerdict(result.get("passed") is True, score, issues)

    def _evaluate_candidates(self, generation: Generation, spec: Any, candidates: list[WorkflowSpec], evaluator: WorkflowEvaluator) -> list[CandidateResult]:
        generation.emit("generation.stage", {"stage": "validating"})
        generation.emit("generation.stage", {"stage": "evaluating"})
        generation.emit("generation.stage", {"stage": "verifying"})
        results: list[CandidateResult] = []
        for index, candidate in enumerate(candidates):
            project = spec.model_copy(deep=True)
            project.workflows = [candidate if item.id == candidate.id else item for item in project.workflows]
            results.append(evaluator.evaluate(project, candidate, index))
        generation.emit("generation.stage", {"stage": "selecting"})
        return results

    @staticmethod
    def _result_feedback(result: CandidateResult) -> dict[str, Any]:
        return {
            "validation_errors": getattr(result, "errors", []),
            "case_failures": [
                {"case_id": case.case_id, "errors": case.errors, "semantic_score": case.semantic_score, "opencode_verified": case.opencode_verified}
                for case in getattr(result, "cases", []) if not case.passed
            ],
        }

    def _apply(self, generation: Generation, operation: dict[str, Any]) -> None:
        action = operation.get("action")
        op_id = operation.get("operation_id") or json.dumps(operation, ensure_ascii=False, sort_keys=True)
        if op_id in generation.operation_ids:
            return
        generation.operation_ids.add(op_id)
        nodes = generation.draft["nodes"]
        edges = generation.draft["edges"]
        node_ids = {item["id"] for item in nodes}
        if action == "add_node":
            node_id = operation["id"]
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", node_id):
                raise ValueError(f"节点编号不合法：{node_id}")
            if node_id in node_ids:
                raise ValueError(f"节点已存在：{node_id}")
            if len(nodes) >= 50:
                raise ValueError("节点数量不能超过 50")
            kind = operation["type"]
            if kind not in WORKFLOW_NODE_TYPES:
                raise ValueError(f"不支持的节点类型：{kind}")
            index = len(nodes)
            data = _operation_data(kind, operation, node_id)
            if data.get("agent_id") and data["agent_id"] not in generation.harness_agent_ids:
                raise ValueError(f"不存在的 Harness 智能体：{data['agent_id']}")
            node = {"id": node_id, "type": kind, "data": data, "position": {"x": 80 + (index % 3) * 260, "y": 80 + (index // 3) * 150}}
            nodes.append(node)
            generation.emit("workflow.node.added", {"node": node})
        elif action == "update_node":
            node = next((item for item in nodes if item["id"] == operation["id"]), None)
            if node is None:
                raise ValueError(f"找不到节点：{operation['id']}")
            updates = operation.get("data") if isinstance(operation.get("data"), dict) else operation
            node["data"].update({key: value for key, value in updates.items() if key in NODE_DATA_FIELDS})
            if node["data"].get("agent_id") and node["data"]["agent_id"] not in generation.harness_agent_ids:
                raise ValueError(f"不存在的 Harness 智能体：{node['data']['agent_id']}")
            generation.emit("workflow.node.updated", {"node": node})
        elif action == "delete_node":
            node_id = operation["id"]
            generation.draft["nodes"] = [item for item in nodes if item["id"] != node_id]
            generation.draft["edges"] = [item for item in edges if item["source"] != node_id and item["target"] != node_id]
            generation.emit("workflow.node.deleted", {"node_id": node_id})
        elif action == "connect_nodes":
            source, target = operation["source"], operation["target"]
            if source == target or source not in node_ids or target not in node_ids:
                raise ValueError(f"无法连接节点：{source} → {target}")
            if len(edges) >= 100:
                raise ValueError("连线数量不能超过 100")
            edge = {"source": source, "target": target}
            if operation.get("condition"):
                edge["condition"] = str(operation["condition"])
            if edge not in edges:
                edges.append(edge)
                generation.emit("workflow.edge.added", {"edge": edge})
        elif action == "disconnect_nodes":
            source, target = operation["source"], operation["target"]
            generation.draft["edges"] = [item for item in edges if not (item["source"] == source and item["target"] == target)]
            generation.emit("workflow.edge.deleted", {"source": source, "target": target})
        elif action == "finalize_workflow":
            self._finalize(generation)
        else:
            raise ValueError(f"未知操作：{action}")

    def _finalize(self, generation: Generation) -> None:
        if generation.completed:
            return
        workflow = WorkflowSpec.model_validate(generation.draft)
        current = self.store.load()
        if self.store.etag() != generation.base_etag:
            generation.completed = True
            generation.emit("generation.conflict", {"message": "工作流在生成期间被修改，请重新发送需求"})
            return
        if generation.mode == "create":
            current.workflows.append(workflow)
        else:
            current.workflows = [workflow if item.id == workflow.id else item for item in current.workflows]
        etag = self.store.save(current, generation.base_etag)
        generation.completed = True
        generation.emit("generation.completed", {"workflow": workflow.model_dump(mode="json"), "etag": etag})


def _find_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get(key), str):
            return value[key]
        for child in value.values():
            found = _find_string(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string(child, key)
            if found:
                return found
    return None


def _command_exceeds_limit(command: list[str]) -> bool:
    configured = os.environ.get("OPENAGENT_COMPACT_COMMAND_LENGTH")
    try:
        limit = int(configured) if configured else (7000 if os.name == "nt" else 120000)
    except ValueError:
        limit = 7000 if os.name == "nt" else 120000
    return len(subprocess.list2cmdline(command)) >= max(1000, limit)


def _with_file_prompt(command: list[str], prompt: str, path: Path) -> list[str]:
    # OpenCode's --file option accepts multiple values greedily, so the positional
    # message must precede it or the message itself is interpreted as a file path.
    return [*command, prompt, "--file", str(path)]


def _is_command_line_too_long(detail: str) -> bool:
    lowered = detail.lower()
    return "command line is too long" in lowered or "命令行太长" in detail or "winerror 206" in lowered


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _redact_log_text(value: Any) -> str:
    """Keep diagnostics useful without writing common credentials to disk."""
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{16,}\b", "<redacted-key>", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret)\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        text,
    )
    return text


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_log_value(item) for key, item in value.items()}
    return value


def _invoke_timeout_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OPENCODE_GENERATOR_CALL_TIMEOUT", "120")), 1800))
    except ValueError:
        return 120


def _planning_timeout_seconds() -> int:
    """Planning calls stay bounded; repair calls use separate shorter bound."""
    return _invoke_timeout_seconds()


def _repair_timeout_seconds() -> int:
    """Keep optimization repair attempts short enough to fail fast.

    Repair prompts are bounded and should not hold the whole optimization loop
    hostage to a slow model. Set OPENCODE_REPAIR_CALL_TIMEOUT to opt into a
    different bound (30..600 seconds).
    """
    try:
        return max(30, min(int(os.environ.get("OPENCODE_REPAIR_CALL_TIMEOUT", "60")), 600))
    except ValueError:
        return 60


def _model_timeout_retry_limit() -> int:
    """Return optional finite timeout retry budget; zero means unlimited."""
    try:
        return max(0, min(int(os.environ.get("OPENAGENT_MODEL_TIMEOUT_RETRIES", "0")), 10000))
    except ValueError:
        return 0


def _incremental_probe_timeout_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OPENAGENT_INCREMENTAL_PROBE_TIMEOUT", "120")), 1800))
    except ValueError:
        return 120


def _incremental_max_iterations() -> int:
    """Bound generation even if model never reaches completion."""
    try:
        return max(1, min(int(os.environ.get("OPENAGENT_INCREMENTAL_MAX_ITERATIONS", "100")), 10000))
    except ValueError:
        return 100


def _agent_loop_max_iterations() -> int:
    """Return optional safety cap; zero means LLM loop continues until success."""
    try:
        return max(0, min(int(os.environ.get("OPENAGENT_AGENT_LOOP_MAX_ITERATIONS", "0")), 100000))
    except ValueError:
        return 0


def _direct_max_attempts() -> int:
    """直出生成最多重试几轮（静态校验失败或验收失败都会计入）。"""
    try:
        return max(1, min(int(os.environ.get("OPENAGENT_DIRECT_MAX_ATTEMPTS", "8")), 100))
    except ValueError:
        return 8


def _modify_build_mode() -> str:
    """修改/修复工作流的生成模式。

    从 OPENAGENT_GENERATOR_MODE 读取，取值：
    - "agent_loop"：LLM Agent Loop（默认）—— 模型自主选择建图、验收、修复、结束
    - "toolcalls"：兼容工具调用模式——模型一次输出多个图操作
    - "chain" / "incremental"：增量链模式（逃生舱）—— 每轮一个语义批次
    - "blueprint"：蓝图直出（整图重写，一般不用于 modify）
    未知或未设置时默认 agent_loop。
    """
    value = os.environ.get("OPENAGENT_GENERATOR_MODE", "agent_loop").strip().lower()
    if value in {"agent_loop", "toolcalls", "chain", "incremental", "blueprint"}:
        return value
    return "agent_loop"


def _create_build_mode() -> str:
    """Creation uses same model-driven loop; legacy modes remain explicit opt-ins."""
    return _modify_build_mode()


def _compaction_timeout_seconds() -> int:
    try:
        return max(30, min(int(os.environ.get("OPENCODE_COMPACTION_TIMEOUT", "30")), 300))
    except ValueError:
        return 30


def _compact_prompt_length() -> int:
    try:
        return max(4000, min(int(os.environ.get("OPENCODE_COMPACT_PROMPT_LENGTH", "12000")), 200000))
    except ValueError:
        return 12000


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    else:
        process.terminate()


def _normalize_workflow_result(
    value: Any,
    harness_agent_ids: set[str],
    *,
    expected_workflow_id: str | None = None,
) -> Any:
    """Accept a small set of common workflow aliases before strict validation."""
    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    if expected_workflow_id and value.get("id") != expected_workflow_id:
        # The route-owned workflow identity is not model-editable. Keep the
        # generated graph while binding its envelope back to the requested
        # workflow so a cosmetic model id change cannot discard the candidate.
        normalized["id"] = expected_workflow_id
    normalized.setdefault("name", str(value.get("title") or value.get("workflow_name") or value.get("id") or "生成工作流"))
    raw_nodes = value.get("nodes")
    if isinstance(raw_nodes, list):
        nodes: list[Any] = []
        available_agents = sorted(harness_agent_ids)
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                nodes.append(raw_node)
                continue
            node = dict(raw_node)
            data = dict(raw_node.get("data")) if isinstance(raw_node.get("data"), dict) else {}
            for alias in ("config", "parameters"):
                if isinstance(raw_node.get(alias), dict):
                    for key, item in raw_node[alias].items():
                        data.setdefault(key, item)
            for key in NODE_DATA_FIELDS:
                if key not in data and key in raw_node:
                    data[key] = raw_node[key]
            if "prompt" not in data and isinstance(data.get("task"), str):
                data["prompt"] = data["task"]
            if "inputs" not in data and isinstance(data.get("input_mapping"), dict):
                data["inputs"] = data["input_mapping"]
            description = str(data.get("description") or raw_node.get("name") or raw_node.get("title") or raw_node.get("id") or "任务")
            data.setdefault("description", description)
            raw_type = str(raw_node.get("type", "")).strip().lower()
            if raw_type in harness_agent_ids:
                node["type"] = "agent"
                data.setdefault("agent_id", raw_type)
            elif raw_type in {"task", "worker", "agent_task", "assistant"}:
                node["type"] = "agent"
                agent: Any = data.get("agent_id") or raw_node.get("agent") or raw_node.get("worker") or raw_node.get("assignee")
                if isinstance(agent, dict):
                    agent = agent.get("id") or agent.get("agent_id")
                if not isinstance(agent, str) or agent not in harness_agent_ids:
                    if len(available_agents) == 1:
                        agent = available_agents[0]
                    else:
                        node_id = raw_node.get("id", "unknown")
                        raise RuntimeError(f"通用 task 节点 {node_id} 无法确定 Harness 智能体，请明确填写 agent_id")
                data["agent_id"] = agent
                task_prompt = raw_node.get("instructions") or raw_node.get("task")
                data.setdefault(
                    "prompt",
                    str(task_prompt or f"你负责{description}。请基于工作流输入与上游结果完成任务。\n\n工作流输入：{{{{input}}}}\n上游结果：{{{{latest}}}}\n\n请输出结构清晰、可供后续节点使用的结果。"),
                )
            elif raw_type in {"human", "user_input", "input", "start", "begin", "trigger"}:
                node["type"] = "manual_trigger"
            elif raw_type in {"human_approval", "manual_approval", "approve"}:
                node["type"] = "approval"
            elif raw_type in {"end", "finish", "terminal", "result", "final"}:
                node["type"] = "output"
            if node.get("type") not in WORKFLOW_NODE_TYPES:
                legal = ", ".join(sorted(WORKFLOW_NODE_TYPES))
                raise RuntimeError(
                    f"不支持的节点类型：{raw_type or '<empty>'}；合法类型：{legal}"
                )
            if node.get("type") == "output" and "template" not in data:
                mapping = data.get("input_mapping")
                if isinstance(mapping, dict) and len(mapping) == 1:
                    data["template"] = next(iter(mapping.values()))
            node["data"] = data
            nodes.append(node)
        normalized["nodes"] = nodes
    raw_edges = value.get("edges")
    if isinstance(raw_edges, list):
        edges: list[Any] = []
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, dict):
                edges.append(raw_edge)
                continue
            edge = dict(raw_edge)
            if "source" not in edge and "from" in edge:
                edge["source"] = edge.pop("from")
            if "target" not in edge and "to" in edge:
                edge["target"] = edge.pop("to")
            edges.append(edge)
        normalized["edges"] = edges
    return normalized


def _project_with_workflow(project: Any, workflow: WorkflowSpec) -> Any:
    candidate = project.model_copy(deep=True)
    replaced = False
    workflows = []
    for item in candidate.workflows:
        if item.id == workflow.id:
            workflows.append(workflow)
            replaced = True
        else:
            workflows.append(item)
    if not replaced:
        workflows.append(workflow)
    candidate.workflows = workflows
    return candidate


_RUNTIME_PROBE_NODE_TYPES = {
    "llm", "agent", "knowledge_retrieval", "tool", "http_request", "code",
    "condition", "switch", "parallel", "iteration", "loop", "approval",
    "validator", "subworkflow",
}


def _canonical_semantic_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_semantic_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in {"position", "summary", "description", "label", "title"}
        }
    if isinstance(value, list):
        return [_canonical_semantic_value(item) for item in value]
    return value


def _semantic_digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical_semantic_value(value), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workflow_semantic_fingerprint(workflow: WorkflowSpec) -> str:
    nodes = sorted(
        ({"id": node.id, "type": node.type, "data": node.data} for node in workflow.nodes),
        key=lambda item: item["id"],
    )
    edges = sorted(
        (
            {"source": edge.source, "target": edge.target, "condition": edge.condition}
            for edge in workflow.edges
        ),
        key=lambda item: (item["source"], item["target"], str(item["condition"])),
    )
    return _semantic_digest({"id": workflow.id, "nodes": nodes, "edges": edges})


def _proposal_semantic_fingerprint(proposal: Any) -> str:
    return _semantic_digest(proposal)


def _failure_semantic_signature(graph_fingerprint: str, failure: dict[str, Any]) -> str:
    # Exact model/error wording is unstable. A no-progress streak is defined by
    # the stable graph plus operation target and failure class, not prose.
    signal = {
        "phase": failure.get("phase"),
        "reason": failure.get("reason"),
        "action": failure.get("action"),
        "node_id": failure.get("node_id"),
    }
    return _semantic_digest({"graph": graph_fingerprint, "failure": signal})


def _proposal_node_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    raw_ids = value.get("node_ids")
    if isinstance(raw_ids, list):
        ids = [str(item).strip() for item in raw_ids if str(item).strip()]
        return ",".join(ids) or None
    node = value.get("node")
    if isinstance(node, dict) and node.get("id") is not None:
        return str(node["id"])
    for key in ("node_id", "id"):
        if value.get(key) is not None:
            return str(value[key])
    return None


def _probe_anchor(touched_node_id: str | None) -> str | None:
    """从逗号分隔的批次节点 id 中取探测锚点（链尾节点）。

    命令链批次会把多个节点 id 用逗号连接；探测只需切片到链尾（最后一个 id），
    即可真实执行整条新增链路。单个节点时等价于原值。
    """
    if not touched_node_id:
        return None
    ids = [item for item in touched_node_id.split(",") if item]
    return ids[-1] if ids else None


def _coerce_action_list(value: Any) -> list[dict[str, Any]]:
    """把工具调用模式的模型输出归一化为操作数组。

    兼容三种形态：
    - 单个操作 dict（历史/退化）
    - 操作数组（标准形态）
    - 带 "operations"/"actions" 键的包装 dict
    """
    if isinstance(value, list):
        actions = value
    elif isinstance(value, dict):
        for key in ("operations", "actions", "ops"):
            if isinstance(value.get(key), list):
                actions = value[key]
                break
        else:
            actions = [value]
    else:
        raise RuntimeError("工具调用结果必须是操作数组或单个操作对象")
    result = [item for item in actions if isinstance(item, dict)]
    if not result:
        raise RuntimeError("工具调用结果没有包含任何有效操作")
    return result


def _step_requires_runtime_probe(
    previous: WorkflowSpec,
    candidate: WorkflowSpec,
    action: str,
    touched_node_id: str | None,
) -> bool:
    if action == "delete_node":
        return True
    touched_ids = {item for item in (touched_node_id or "").split(",") if item}
    if any(
        node.id in touched_ids and node.type in _RUNTIME_PROBE_NODE_TYPES
        for node in candidate.nodes
    ):
        return True
    previous_conditions = {
        (edge.source, edge.target, edge.condition) for edge in previous.edges if edge.condition
    }
    candidate_conditions = {
        (edge.source, edge.target, edge.condition) for edge in candidate.edges if edge.condition
    }
    return previous_conditions != candidate_conditions


def _edge_key(value: Any) -> tuple[str, str, str | None]:
    if isinstance(value, dict):
        return str(value.get("source", "")), str(value.get("target", "")), value.get("condition")
    return value.source, value.target, value.condition


def _dag_errors(workflow: WorkflowSpec) -> list[str]:
    """Validate graph shape only; partial DAGs may omit output/runtime fields."""
    node_ids = [node.id for node in workflow.nodes]
    known = set(node_ids)
    errors: list[str] = []
    if len(node_ids) != len(known):
        errors.append("工作流包含重复节点编号")
    indegree = {node_id: 0 for node_id in known}
    outgoing = {node_id: [] for node_id in known}
    seen_edges: set[tuple[str, str, str | None]] = set()
    for edge in workflow.edges:
        key = (edge.source, edge.target, edge.condition)
        if key in seen_edges:
            errors.append("工作流包含重复连线")
        seen_edges.add(key)
        if edge.source not in known or edge.target not in known:
            errors.append(f"连线引用了不存在的节点：{edge.source} → {edge.target}")
            continue
        if edge.source == edge.target:
            errors.append(f"不允许自环连线：{edge.source}")
            continue
        indegree[edge.target] += 1
        outgoing[edge.source].append(edge.target)
    if errors:
        return list(dict.fromkeys(errors))
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        node_id = queue.pop()
        visited += 1
        for target in outgoing[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(known):
        errors.append("工作流包含图循环；请使用循环节点表达有限次数循环")
    return errors


def _apply_creation_step(
    current: WorkflowSpec,
    value: Any,
    harness_agent_ids: set[str],
) -> tuple[WorkflowSpec, str, str | None, str]:
    if not isinstance(value, dict):
        raise RuntimeError("创建步骤必须是 JSON 对象")
    action = str(value.get("action", "")).strip()
    if action not in {"add_node", "revise_node", "finish_creation"}:
        raise RuntimeError("创建 action 必须是 add_node、revise_node 或 finish_creation")
    summary = str(value.get("summary", "")).strip() or action
    if action == "finish_creation":
        return current.model_copy(deep=True), action, None, summary
    if not isinstance(value.get("probe_approvals", {}), dict):
        raise RuntimeError("probe_approvals 必须是审批节点到布尔值的对象")
    raw_node = value.get("node")
    if not isinstance(raw_node, dict):
        raise RuntimeError(f"{action} 缺少完整 node 对象")
    add_edges = value.get("add_edges", [])
    remove_edges = value.get("remove_edges", [])
    if not isinstance(add_edges, list) or not isinstance(remove_edges, list):
        raise RuntimeError("add_edges 和 remove_edges 必须是数组")
    normalized = _normalize_workflow_result({
        "id": current.id,
        "name": current.name,
        "nodes": [raw_node],
        "edges": add_edges,
    }, harness_agent_ids, expected_workflow_id=current.id)
    step = WorkflowSpec.model_validate(normalized)
    node = step.nodes[0]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", node.id):
        raise RuntimeError(f"节点编号不合法：{node.id}")
    if node.data.get("agent_id") and node.data["agent_id"] not in harness_agent_ids:
        raise RuntimeError(f"不存在的 Harness 智能体：{node.data['agent_id']}")
    draft = current.model_dump(mode="json")
    existing_ids = {item["id"] for item in draft["nodes"]}
    if action == "add_node":
        if node.id in existing_ids:
            raise RuntimeError(f"新增节点已存在：{node.id}")
        if len(draft["nodes"]) >= 50:
            raise RuntimeError("节点数量不能超过 50")
        if remove_edges:
            raise RuntimeError("add_node 不允许 remove_edges")
        draft["nodes"].append(node.model_dump(mode="json"))
    else:
        if node.id not in existing_ids:
            raise RuntimeError(f"修复目标节点不存在：{node.id}")
        edges_mode = str(value.get("edges_mode", "preserve"))
        if edges_mode not in {"preserve", "patch"}:
            raise RuntimeError("revise_node 的 edges_mode 必须是 preserve 或 patch")
        if edges_mode == "preserve" and (remove_edges or add_edges):
            raise RuntimeError("edges_mode=preserve 时不能修改连线")
        draft["nodes"] = [node.model_dump(mode="json") if item["id"] == node.id else item for item in draft["nodes"]]
        if edges_mode == "patch":
            remove_keys = {_edge_key(edge) for edge in remove_edges}
            existing_keys = {_edge_key(edge) for edge in draft["edges"]}
            missing = remove_keys - existing_keys
            if missing:
                raise RuntimeError(f"待删除连线不存在：{sorted(missing)}")
            draft["edges"] = [edge for edge in draft["edges"] if _edge_key(edge) not in remove_keys]
    for edge in step.edges:
        if node.id not in {edge.source, edge.target}:
            raise RuntimeError(f"创建步骤连线必须连接当前节点 {node.id}")
        dumped = edge.model_dump(mode="json", exclude_none=True)
        if dumped not in draft["edges"]:
            draft["edges"].append(dumped)
    if isinstance(value.get("workflow_name"), str) and value["workflow_name"].strip():
        draft["name"] = value["workflow_name"].strip()
    return WorkflowSpec.model_validate(draft), action, node.id, summary


def _creation_step_errors(
    previous: WorkflowSpec,
    candidate: WorkflowSpec,
    action: str,
    touched_node_id: str | None,
) -> list[str]:
    errors: list[str] = []
    node_ids = {node.id for node in candidate.nodes}
    edge_keys = [_edge_key(edge) for edge in candidate.edges]
    if len(edge_keys) != len(set(edge_keys)):
        errors.append("工作流包含重复连线")
    for edge in candidate.edges:
        if edge.source not in node_ids or edge.target not in node_ids:
            errors.append(f"连线引用不存在节点：{edge.source} → {edge.target}")
        if edge.source == edge.target:
            errors.append(f"不允许自环连线：{edge.source}")
    if action == "add_node" and previous.nodes and touched_node_id:
        if not any(touched_node_id in {edge.source, edge.target} for edge in candidate.edges):
            errors.append(f"新增节点 {touched_node_id} 没有接入当前草稿")
    if len(candidate.edges) > 100:
        errors.append("连线数量不能超过 100")
    for node in candidate.nodes:
        if node.type != "condition":
            continue
        expression = str(node.data.get("expression") or "").strip()
        if not expression:
            errors.append(f"条件节点 {node.id} 缺少 expression")
        elif "{{" in expression or "}}" in expression:
            errors.append(
                f"条件节点 {node.id} 的 expression 不支持模板大括号；"
                "引用直接上游审批结果请使用 latest.approved == true"
            )
    node_types = {node.id: node.type for node in candidate.nodes}
    for edge in candidate.edges:
        if node_types.get(edge.source) == "condition" and str(edge.condition or "").strip() not in {"true", "false"}:
            errors.append(
                f"条件节点 {edge.source} 的出边 {edge.target} 必须使用 true 或 false，"
                f"不能使用 {edge.condition or '空条件'}"
            )
    return list(dict.fromkeys(errors))


def _apply_incremental_step(
    current: WorkflowSpec,
    value: Any,
    harness_agent_ids: set[str],
    *,
    allow_delete: bool = True,
) -> tuple[WorkflowSpec, str, str | None, Any, dict[str, bool], str]:
    if not isinstance(value, dict):
        raise RuntimeError("增量构建结果必须是 JSON 对象")
    action = str(value.get("action", "")).strip()
    if action not in {"add_node", "update_node", "delete_node", "complete"}:
        raise RuntimeError("增量构建 action 必须是 add_node、update_node、delete_node 或 complete")
    summary = str(value.get("summary", "")).strip() or action
    if action == "complete":
        return current.model_copy(deep=True), action, None, value.get("probe_input"), {}, summary

    draft = current.model_dump(mode="json")
    if isinstance(value.get("workflow_name"), str) and value["workflow_name"].strip():
        draft["name"] = value["workflow_name"].strip()
    touched_node_id: str | None = None
    if action == "delete_node":
        if not allow_delete:
            raise RuntimeError("当前操作不允许删除节点")
        raw_node_ids = value.get("node_ids")
        if raw_node_ids is None:
            raw_node_ids = [value.get("node_id")]
        if not isinstance(raw_node_ids, list):
            raise RuntimeError("delete_node 的 node_ids 必须是节点 id 数组")
        node_ids = list(dict.fromkeys(str(item or "").strip() for item in raw_node_ids))
        if not node_ids or any(not item for item in node_ids):
            raise RuntimeError("delete_node 必须包含至少一个非空节点 id")
        existing_ids = {node["id"] for node in draft["nodes"]}
        missing_ids = [node_id for node_id in node_ids if node_id not in existing_ids]
        if missing_ids:
            raise RuntimeError(f"删除目标节点不存在：{', '.join(missing_ids)}")
        deleting_ids = set(node_ids)
        if deleting_ids == existing_ids:
            raise RuntimeError(
                "不能删除全部当前节点：整体重建必须先 add_node 创建并连通新工作流，"
                "确认新图可运行后才能批量删除旧节点；当前图不得变为空图"
            )
        touched_node_id = ",".join(node_ids)
        draft["nodes"] = [node for node in draft["nodes"] if node["id"] not in deleting_ids]
        draft["edges"] = [
            edge for edge in draft["edges"]
            if edge["source"] not in deleting_ids and edge["target"] not in deleting_ids
        ]
    else:
        # add_node/update_node：支持单个 node（历史兼容）或 nodes 数组（命令链批次）。
        # 命令链批次借鉴 Maestro-Flow 的语义宏操作：一次产出多个关联节点 + 连线，
        # 一次应用、一次验证，避免「每轮一个节点」的过度约束。
        raw_node = value.get("node")
        raw_nodes = value.get("nodes")
        if isinstance(raw_nodes, list):
            if not raw_nodes or any(not isinstance(item, dict) for item in raw_nodes):
                raise RuntimeError(f"{action} 的 nodes 必须是非空节点对象数组")
            raw_node_list = raw_nodes
        elif isinstance(raw_node, dict):
            raw_node_list = [raw_node]
        else:
            raise RuntimeError(f"{action} 缺少完整 node 对象（或 nodes 数组）")
        if action == "update_node" and len(raw_node_list) != 1:
            raise RuntimeError("update_node 一次只能更新一个节点；多个节点请拆成多个 add_node 批次")
        raw_edges = value.get("edges", [])
        if not isinstance(raw_edges, list):
            raise RuntimeError("增量构建 edges 必须是数组")
        normalized_step = _normalize_workflow_result({
            "id": current.id,
            "name": current.name,
            "nodes": raw_node_list,
            "edges": raw_edges,
        }, harness_agent_ids, expected_workflow_id=current.id)
        step_workflow = WorkflowSpec.model_validate(normalized_step)
        new_nodes = step_workflow.nodes
        new_ids = [node.id for node in new_nodes]
        for node_id in new_ids:
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", node_id):
                raise RuntimeError(f"节点编号不合法：{node_id}")
        if len(new_ids) != len(set(new_ids)):
            raise RuntimeError(f"本批节点编号重复：{new_ids}")
        touched_node_id = ",".join(new_ids)
        existing_ids = {item["id"] for item in draft["nodes"]}
        if action == "add_node":
            duplicates = [node_id for node_id in new_ids if node_id in existing_ids]
            if duplicates:
                raise RuntimeError(f"新增节点已存在：{', '.join(duplicates)}")
            if len(draft["nodes"]) + len(new_nodes) > 50:
                raise RuntimeError("节点数量不能超过 50；请更新或删除现有节点")
            for node in new_nodes:
                draft["nodes"].append(node.model_dump(mode="json"))
        else:
            node = new_nodes[0]
            if node.id not in existing_ids:
                raise RuntimeError(f"更新目标节点不存在：{node.id}")
            draft["nodes"] = [
                node.model_dump(mode="json") if item["id"] == node.id else item
                for item in draft["nodes"]
            ]
            if "edges" in value:
                draft["edges"] = [
                    edge for edge in draft["edges"]
                    if edge["source"] != node.id and edge["target"] != node.id
                ]
        batch_id_set = set(new_ids)
        for edge in step_workflow.edges:
            if not any(node_id in {edge.source, edge.target} for node_id in new_ids):
                raise RuntimeError(f"本步连线必须连接本批新增节点之一：{edge.source} → {edge.target}")
            dumped = edge.model_dump(mode="json", exclude_none=True)
            if dumped not in draft["edges"]:
                draft["edges"].append(dumped)
        # 批次闭合校验：每个新节点都必须（直接或经其它新节点）接入既有图。
        # 既有可能存在既有图时的孤立批次，也有空图时的首节点批次。
        if action == "add_node" and len(new_ids) > 1:
            outgoing_within_batch = {
                node_id for node_id in new_ids
                if any(edge.source == node_id and edge.target in batch_id_set and edge.target != node_id for edge in step_workflow.edges)
            }
            incoming_from_existing = {
                node_id for node_id in new_ids
                if any(edge.target == node_id and edge.source not in batch_id_set for edge in step_workflow.edges)
            }
            # 批次内不能出现内部环（每个新节点最多作为一条内部链的一环）；此处只做弱校验，
            # 强校验交给 _incremental_connectivity_errors 的全图连通检查。
            if len(new_ids) - len(outgoing_within_batch) > 1:
                raise RuntimeError("命令链批次必须是一条有向链：一次最多只能有一个节点作为链尾（无内部出边）")
            if not incoming_from_existing and existing_ids:
                raise RuntimeError("命令链批次必须通过至少一条连线接入既有工作流")

    candidate = WorkflowSpec.model_validate(draft)
    approvals_value = value.get("probe_approvals", {})
    if approvals_value is None:
        approvals_value = {}
    if not isinstance(approvals_value, dict):
        raise RuntimeError("probe_approvals 必须是审批节点到布尔值的对象")
    probe_approvals = {str(key): bool(item) for key, item in approvals_value.items()}
    return candidate, action, touched_node_id, value.get("probe_input"), probe_approvals, summary


def _incremental_connectivity_errors(
    project: Any,
    previous: WorkflowSpec,
    candidate: WorkflowSpec,
    action: str,
    touched_node_id: str | None,
    *,
    require_output: bool,
) -> list[str]:
    errors = validate_executable_workflow(project, candidate, runtime=True, require_output=require_output)
    node_ids = {node.id for node in candidate.nodes}
    edge_keys = [(edge.source, edge.target, edge.condition) for edge in candidate.edges]
    if len(edge_keys) != len(set(edge_keys)):
        errors.append("工作流包含重复连线")
    if action == "add_node" and previous.nodes and touched_node_id:
        touched_ids = {item for item in touched_node_id.split(",") if item}
        if touched_ids and not any(
            node_id in {edge.source, edge.target} for edge in candidate.edges for node_id in touched_ids
        ):
            errors.append(f"新增节点 {touched_node_id} 没有接入当前工作流")
    if node_ids:
        adjacency = {node_id: set() for node_id in node_ids}
        for edge in candidate.edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)
        visited: set[str] = set()
        stack = [next(iter(node_ids))]
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            stack.extend(adjacency[node_id] - visited)
        disconnected = sorted(node_ids - visited)
        if disconnected:
            errors.append(f"工作流存在未连通节点：{', '.join(disconnected)}")
    if require_output:
        output_ids = {node.id for node in candidate.nodes if node.type == "output"}
        if not output_ids:
            errors.append("完整工作流至少需要一个 output 节点")
        sources = {edge.source for edge in candidate.edges}
        invalid_sinks = sorted(
            node.id for node in candidate.nodes
            if node.id not in sources and node.type != "output"
        )
        if invalid_sinks:
            errors.append(f"完整工作流的终点必须是 output 节点：{', '.join(invalid_sinks)}")
    return list(dict.fromkeys(errors))


def _explicit_delete_request(prompt: str) -> bool:
    normalized = re.sub(r"\s+", "", prompt.lower())
    delete_verbs = r"(?:删除|移除|删掉|去掉|delete|remove)"
    if re.search(rf"(?:不要|别|勿|禁止|不许|不能|不需|不用|无需|无须|莫)[要]?{delete_verbs}", normalized):
        return False
    targets = r"(?:节点|node|工作流|workflow)"
    return bool(
        re.search(rf"{delete_verbs}(?:[^。！？\n]{{0,80}}){targets}", prompt, re.IGNORECASE)
        or re.search(rf"{targets}(?:[^。！？\n]{{0,80}}){delete_verbs}", prompt, re.IGNORECASE)
    )


def _incremental_trigger_for_node(workflow: WorkflowSpec, touched_node_id: str | None) -> str | None:
    nodes = {node.id: node for node in workflow.nodes}
    parents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in workflow.edges:
        if edge.target in parents:
            parents[edge.target].append(edge.source)
    queue_ids = [touched_node_id] if touched_node_id in nodes else list(nodes)
    visited: set[str] = set()
    while queue_ids:
        node_id = queue_ids.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        node = nodes[node_id]
        if node.type in {"webhook", "schedule"}:
            return node_id
        queue_ids.extend(parents[node_id])
    return None


def _incremental_probe_workflow(workflow: WorkflowSpec, touched_node_id: str | None) -> WorkflowSpec:
    """Return smallest executable layer slice ending at touched node."""
    nodes = {node.id: node for node in workflow.nodes}
    if not touched_node_id or touched_node_id not in nodes:
        return workflow.model_copy(deep=True)
    parents: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for edge in workflow.edges:
        if edge.source in nodes and edge.target in parents:
            parents[edge.target].append(edge.source)
    queue: list[tuple[str, list[str]]] = [(touched_node_id, [touched_node_id])]
    visited: set[str] = set()
    selected_path = [touched_node_id]
    while queue:
        node_id, reverse_path = queue.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        if node_id != touched_node_id and nodes[node_id].type == "approval":
            selected_path = list(reversed(reverse_path))
            break
        queue.extend((parent, reverse_path + [parent]) for parent in parents[node_id])
    selected = set(selected_path)
    draft = workflow.model_dump(mode="json")
    draft["nodes"] = [node.model_dump(mode="json") for node in workflow.nodes if node.id in selected]
    draft["edges"] = [
        edge.model_dump(mode="json")
        for edge in workflow.edges
        if edge.source in selected and edge.target in selected
    ]
    return WorkflowSpec.model_validate(draft)


def _is_harness_infrastructure_message(message: str) -> bool:
    return str(message).startswith((
        "Harness 不可用：",
        "Harness 基础设施错误：",
        "Harness 任务 API 契约不兼容：",
        "Harness 请求失败（502）",
        "Harness 请求失败（503）",
        "Harness 请求失败（504）",
        "environment drift",
        "setup required",
        "setup_required",
        "agent environment setup is required",
        "agent setup or runtime is in error",
    )) or any(f"code={code}" in str(message) for code in {
        "setup_required", "agent_process_failed", "agent_timeout", "agent_permission_denied",
        "sandbox_unavailable", "sandbox_denied", "protocol_output_invalid",
    })


def _normalize_evaluation_result(value: Any) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("cases"), list):
        return value
    normalized = dict(value)
    cases: list[Any] = []
    used_ids: set[str] = set()
    for index, raw_case in enumerate(value["cases"], start=1):
        if not isinstance(raw_case, dict):
            cases.append(raw_case)
            continue
        case = dict(raw_case)
        raw_id = case.get("id")
        case_id = str(raw_id).strip().lower() if raw_id is not None else ""
        case_id = re.sub(r"[^a-z0-9]+", "-", case_id).strip("-") or f"case-{index}"
        base_case_id = case_id
        suffix = 2
        while case_id in used_ids:
            case_id = f"{base_case_id}-{suffix}"
            suffix += 1
        used_ids.add(case_id)
        case["id"] = case_id
        criteria = case.get("semantic_criteria")
        if isinstance(criteria, str):
            case["semantic_criteria"] = [criteria]
        elif isinstance(criteria, list):
            normalized_criteria: list[Any] = []
            for criterion in criteria:
                if isinstance(criterion, dict):
                    text = next((
                        criterion.get(key) for key in ("description", "criterion", "text", "content")
                        if isinstance(criterion.get(key), str)
                    ), None)
                    normalized_criteria.append(text if text is not None else criterion)
                else:
                    normalized_criteria.append(criterion)
            case["semantic_criteria"] = normalized_criteria
        assertions = case.get("assertions")
        if isinstance(assertions, dict):
            case["assertions"] = [assertions]
        approvals = case.get("approvals")
        if isinstance(approvals, list):
            decisions: dict[str, bool] = {}
            for item in approvals:
                if isinstance(item, str):
                    decisions[item] = True
                elif isinstance(item, dict):
                    node_id = item.get("node_id") or item.get("id")
                    if isinstance(node_id, str):
                        decisions[node_id] = item.get("approved", True) is not False
            case["approvals"] = decisions
        mocks = case.get("mocks")
        if isinstance(mocks, dict):
            case["mocks"] = [{"node_id": str(node_id), "response": response} for node_id, response in mocks.items()]
        elif isinstance(mocks, list):
            normalized_mocks: list[Any] = []
            for mock in mocks:
                if not isinstance(mock, dict):
                    normalized_mocks.append(mock)
                    continue
                normalized_mock = dict(mock)
                if "node_id" not in normalized_mock:
                    node_id = normalized_mock.get("target") or normalized_mock.get("id")
                    if node_id is not None:
                        normalized_mock["node_id"] = str(node_id)
                normalized_mocks.append(normalized_mock)
            case["mocks"] = normalized_mocks
        timeout_seconds = case.get("timeout_seconds")
        if isinstance(timeout_seconds, int) and not isinstance(timeout_seconds, bool):
            case["timeout_seconds"] = max(1, min(timeout_seconds, 1800))
        elif isinstance(timeout_seconds, str) and re.fullmatch(r"[+-]?\d+", timeout_seconds.strip()):
            case["timeout_seconds"] = max(1, min(int(timeout_seconds), 1800))
        cases.append(case)
    normalized["cases"] = cases
    return normalized


def _parse_result(text: str) -> Any:
    tagged = re.findall(r"<result>\s*(.*?)\s*</result>", text, re.DOTALL | re.IGNORECASE)
    payloads = [*tagged, text] if tagged else [text]
    for payload in payloads:
        cleaned = payload.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        try:
            value = json.loads(cleaned)
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    pass
            return value
        except json.JSONDecodeError:
            pass
        embedded = _largest_embedded_json(cleaned)
        if embedded is not None:
            return embedded
    raise StructuredResultError("OpenCode 未返回有效的结构化 JSON 结果")


def _largest_embedded_json(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    best_value: Any | None = None
    best_length = -1
    for match in re.finditer(r"[\[{]", text):
        try:
            value, end = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if end >= best_length:
            best_value = value
            best_length = end
    return best_value


def _extract_text(item: dict[str, Any]) -> str:
    part = item.get("part") or item.get("properties", {}).get("part")
    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
        return part["text"]
    if item.get("type") == "text" and isinstance(item.get("text"), str):
        return item["text"]
    return ""


def _extract_error(item: dict[str, Any]) -> str:
    event_type = str(item.get("type", "")).lower()
    if "error" not in event_type and not item.get("error"):
        return ""
    value: Any = item.get("error") or item.get("properties") or item
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, dict):
        # 保留 @ai-sdk APICallError 的完整证据（statusCode / data.message / cause），
        # 否则只会看到裸 "APIError"，无法定位是模型不存在还是鉴权失败。
        parts: list[str] = []
        for key in ("message", "name", "statusCode", "status", "code"):
            if value.get(key) is not None:
                parts.append(f"{key}={value[key]}")
        nested = value.get("data")
        if isinstance(nested, dict):
            for key in ("message", "type", "code", "error"):
                if nested.get(key) is not None:
                    parts.append(f"data.{key}={nested[key]}")
        cause = value.get("cause")
        if isinstance(cause, dict):
            for key in ("message", "name", "statusCode", "code"):
                if cause.get(key) is not None:
                    parts.append(f"cause.{key}={cause[key]}")
        if parts:
            return "; ".join(str(p) for p in parts)[:2000]
        return json.dumps(value, ensure_ascii=False)[:2000]
    return "OpenCode 返回错误事件"


def _operation_data(kind: str, operation: dict[str, Any], node_id: str) -> dict[str, Any]:
    supplied = operation.get("data") if isinstance(operation.get("data"), dict) else operation
    description = str(supplied.get("description") or operation.get("description") or node_id)
    data = {key: value for key, value in supplied.items() if key in NODE_DATA_FIELDS}
    data["description"] = description
    if kind in {"llm", "agent", "tool", "code"}:
        agent_id = data.get("agent_id") or operation.get("agent_id")
        if not agent_id:
            raise ValueError(f"智能体节点 {node_id} 缺少 agent_id")
        data["agent_id"] = agent_id
        data.setdefault("prompt", f"你负责{description}。请基于工作流输入与上游结果完成任务。\n\n工作流输入：{{{{input}}}}\n上游结果：{{{{latest}}}}\n\n请输出结构清晰、可供后续节点使用的结果。")
    elif kind == "prompt":
        data.setdefault("template", f"{description}\n\n输入：{{{{input}}}}\n上游结果：{{{{latest}}}}")
    elif kind == "knowledge_retrieval":
        data.setdefault("query", "{{latest}}")
        data.setdefault("top_k", 3)
        data.setdefault("documents", [])
    elif kind == "http_request":
        data.setdefault("method", "GET")
        data.setdefault("headers", {})
        data.setdefault("timeout_seconds", 30)
        data.setdefault("fail_on_error", True)
    elif kind == "variable_set":
        data.setdefault("variables", {})
    elif kind == "transform":
        data.setdefault("operation", "json_stringify")
    elif kind == "merge":
        data.setdefault("mode", "array")
    elif kind == "condition":
        data.setdefault("expression", "latest")
    elif kind == "switch":
        data.setdefault("cases", [])
        data.setdefault("default_case", "default")
    elif kind in {"iteration", "loop"}:
        data.setdefault("iterations", 3)
        data.setdefault("template", f"{description}（第 {{{{index}}}} 次）\n{{{{latest}}}}")
    elif kind == "validator" and data.get("agent_id"):
        data.setdefault("prompt", f"你负责{description}。请验证以下上游结果并明确给出是否通过、问题清单和修正建议：\n\n{{{{latest}}}}")
    elif kind == "webhook":
        data.setdefault("path", f"/hooks/{node_id}")
        data.setdefault("method", "POST")
    elif kind == "schedule":
        data.setdefault("cron", "0 9 * * *")
        data.setdefault("timezone", "Asia/Shanghai")
    elif kind == "approval":
        data.setdefault("instructions", description)
    elif kind == "delay":
        data.setdefault("seconds", 1)
    return data
