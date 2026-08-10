import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, MiniMap, Panel, ReactFlow, ReactFlowProvider,
  SelectionMode, addEdge, applyEdgeChanges, applyNodeChanges, getBezierPath, useReactFlow,
  type Connection, type Edge, type EdgeChange, type EdgeProps, type EdgeTypes, type Node, type NodeChange,
} from '@xyflow/react'
import { ApiError, cancelGeneration, cancelWorkflowRun, loadGeneratorMessages, loadGeneratorStatus, loadIntegrationsStatus, loadRuntimeStatus, loadSpec, optimizeWorkflow, resolveApproval, saveWorkflow, sendGeneratorMessage, startWorkflowRun } from './api'
import type { EvaluationCase, IntegrationsStatus, NodeKind, ProjectSpec, RuntimeStatus, Workflow, WorkflowRun } from './types'

type CatalogItem = { type: NodeKind; category: string; label: string; icon: string; description: string }
const nodeCatalog: CatalogItem[] = [
  { type: 'manual_trigger', category: '触发器', label: '手动触发', icon: '▶', description: '从表单或 API 输入启动流程' },
  { type: 'webhook', category: '触发器', label: 'Webhook', icon: '⚡', description: '通过 HTTP Webhook 触发' },
  { type: 'schedule', category: '触发器', label: '定时触发', icon: '◷', description: '使用 Cron 计划触发' },
  { type: 'llm', category: 'AI', label: 'LLM', icon: '◆', description: '调用模型完成单轮生成' },
  { type: 'agent', category: 'AI', label: '智能体', icon: '✦', description: '调用 Harness 智能体完成任务' },
  { type: 'knowledge_retrieval', category: 'AI', label: '知识检索', icon: '⌕', description: '从知识文档召回相关内容' },
  { type: 'tool', category: 'AI', label: '工具调用', icon: '⌘', description: '通过 Harness 执行受治理工具' },
  { type: 'code', category: 'AI', label: '代码任务', icon: '</>', description: '通过 Harness Agent 执行代码任务' },
  { type: 'prompt', category: '数据处理', label: '模板', icon: 'T', description: '组织提示词或文本模板' },
  { type: 'variable_set', category: '数据处理', label: '变量赋值', icon: 'x=', description: '创建工作流变量对象' },
  { type: 'transform', category: '数据处理', label: '数据转换', icon: '⇄', description: '解析、提取、筛选或扁平化数据' },
  { type: 'merge', category: '数据处理', label: '合并', icon: '⋈', description: '合并多个上游分支结果' },
  { type: 'http_request', category: '集成', label: 'HTTP 请求', icon: '◎', description: '调用 HTTPS API' },
  { type: 'condition', category: '流程控制', label: 'IF/ELSE', icon: '◇', description: '根据布尔结果选择分支' },
  { type: 'switch', category: '流程控制', label: '多路分支', icon: '⑂', description: '按多个条件路由到不同分支' },
  { type: 'parallel', category: '流程控制', label: '并行', icon: '⋮', description: '并行调度互不依赖的分支' },
  { type: 'iteration', category: '流程控制', label: '迭代', icon: '↻', description: '遍历或按次数生成迭代项' },
  { type: 'loop', category: '流程控制', label: '循环', icon: '⟳', description: '执行有限次数循环' },
  { type: 'delay', category: '流程控制', label: '等待', icon: '◴', description: '延迟后继续执行' },
  { type: 'approval', category: '人工与质量', label: '人工审批', icon: '✓', description: '暂停并等待人工决定' },
  { type: 'validator', category: '人工与质量', label: '验证器', icon: '⌁', description: '验证任务终态或业务规则' },
  { type: 'subworkflow', category: '编排', label: '子工作流', icon: '▣', description: '调用另一个工作流' },
  { type: 'output', category: '输出', label: '结束/输出', icon: '→', description: '定义工作流最终输出' },
]

const nodeDefaults: Partial<Record<NodeKind, Record<string, unknown>>> = {
  webhook: { path: '/hooks/workflow', method: 'POST' }, schedule: { cron: '0 9 * * *', timezone: 'Asia/Shanghai' },
  llm: { prompt: '请基于以下输入完成任务：\n{{latest}}' }, agent: { prompt: '请完成以下任务：\n{{latest}}', relative_path: '.' },
  knowledge_retrieval: { query: '{{latest}}', top_k: 3, documents: [] }, tool: { prompt: '请调用合适的工具处理：\n{{latest}}' },
  code: { prompt: '请完成代码任务并运行验证：\n{{latest}}', relative_path: '.' }, prompt: { template: '{{latest}}' },
  variable_set: { variables: {} }, transform: { operation: 'json_stringify', path: '', fields: [] }, merge: { mode: 'array', separator: '\n' },
  http_request: { method: 'GET', url: 'https://', headers: {}, body: {}, timeout_seconds: 30, fail_on_error: true },
  condition: { expression: 'latest' }, switch: { cases: [{ value: 'case-1', expression: 'latest == "value"' }], default_case: 'default' },
  iteration: { iterations: 3, template: '{{latest}}' }, loop: { iterations: 3, template: '{{latest}}' }, delay: { seconds: 1 },
  approval: { instructions: '请检查上游结果并决定是否继续' }, validator: { expression: '' }, subworkflow: { input_template: '{{latest}}' }, output: { template: '' },
}
const harnessNodeKinds: NodeKind[] = ['llm', 'agent', 'tool', 'code', 'validator']

type CanvasData = { label: string; description: string; kind: NodeKind; agent_id?: string; [key: string]: unknown }
type CanvasNode = Node<CanvasData>
type ChatMessage = { role: 'user' | 'assistant' | 'system'; content: string; options?: string[] }

const nodeStyle = { background: 'rgba(255,255,255,.055)', color: 'rgba(255,255,255,.92)', border: 'none', borderRadius: 16, width: 200, boxShadow: '0 12px 32px rgba(0,0,0,.42), inset 0 1px 1px rgba(255,255,255,.12)', fontSize: 12 }

function WaterEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, markerEnd, label, interactionWidth }: EdgeProps) {
  const [path, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, curvature: .38 })
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={interactionWidth} className="water-edge-channel"/>
    <path d={path} className="water-edge-flow" aria-hidden="true"/>
    {label != null && label !== '' && <EdgeLabelRenderer><div className="water-edge-label nodrag nopan" style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}>{String(label)}</div></EdgeLabelRenderer>}
  </>
}

const edgeTypes: EdgeTypes = { water: WaterEdge }

function nodeLabel(kind: NodeKind) {
  return nodeCatalog.find((item) => item.type === kind)?.label ?? kind
}

function toCanvas(workflow: Workflow): { nodes: CanvasNode[]; edges: Edge[] } {
  return {
    nodes: workflow.nodes.map((item) => ({
      id: item.id, type: 'default', position: item.position,
      data: {
        ...item.data,
        label: String(item.data.description || nodeLabel(item.type)),
        description: String(item.data.description || ''), kind: item.type,
        agent_id: typeof item.data.agent_id === 'string' ? item.data.agent_id : undefined,
      },
      style: nodeStyle,
    })),
    edges: workflow.edges.map((item, index) => ({ id: `e-${index}-${item.source}-${item.target}`, source: item.source, target: item.target, label: item.condition || undefined, type: 'water' })),
  }
}

function StudioCanvas() {
  const [spec, setSpec] = useState<ProjectSpec | null>(null)
  const [etag, setEtag] = useState('')
  const [workflowId, setWorkflowId] = useState('')
  const [nodes, setNodes] = useState<CanvasNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null)
  const [message, setMessage] = useState('正在载入项目…')
  const [saving, setSaving] = useState(false)
  const [rightTab, setRightTab] = useState<'chat' | 'node' | 'edge' | 'evaluation' | 'run'>('chat')
  const [chatInput, setChatInput] = useState('')
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [generationId, setGenerationId] = useState<string | null>(null)
  const [generatorModel, setGeneratorModel] = useState('正在读取模型…')
  const [run, setRun] = useState<WorkflowRun | null>(null)
  const [runInput, setRunInput] = useState('')
  const [runEvents, setRunEvents] = useState<string[]>([])
  const [libraryQuery, setLibraryQuery] = useState('')
  const [integrations, setIntegrations] = useState<IntegrationsStatus>({ feishu: [], qq: [] })
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null)
  const { screenToFlowPosition, fitView } = useReactFlow()

  const workflow = useMemo(() => spec?.workflows.find((item) => item.id === workflowId), [spec, workflowId])
  const selectedNode = nodes.find((item) => item.id === selected)
  const selectedHarness = spec?.harness.find((item) => item.id === selectedNode?.data.agent_id)
  const currentEdge = edges.find((item) => item.id === selectedEdge)
  const selectedNodeCount = nodes.filter((item) => item.selected).length
  const selectedEdgeCount = edges.filter((item) => item.selected).length
  const selectionCount = selectedNodeCount + selectedEdgeCount
  const visibleCatalog = useMemo(() => {
    const query = libraryQuery.trim().toLowerCase()
    if (!query) return nodeCatalog
    return nodeCatalog.filter((item) => [item.label, item.type, item.category, item.description].some((value) => value.toLowerCase().includes(query)))
  }, [libraryQuery])

  const openWorkflow = useCallback((project: ProjectSpec, id: string) => {
    const item = project.workflows.find((flow) => flow.id === id)
    if (!item) return
    const view = toCanvas(item)
    setNodes(view.nodes); setEdges(view.edges); setWorkflowId(id); setSelected(null); setSelectedEdge(null); setRun(null)
    loadGeneratorMessages(id).then((items) => setChatMessages(items)).catch(() => setChatMessages([]))
    setTimeout(() => fitView({ padding: 0.22 }), 40)
  }, [fitView])

  useEffect(() => {
    loadSpec().then(({ spec: project, etag: tag }) => {
      setSpec(project); setEtag(tag)
      if (project.workflows.length) openWorkflow(project, project.workflows[0].id)
      setMessage('项目已就绪')
    }).catch((error: Error) => setMessage(error.message))
  }, [openWorkflow])

  useEffect(() => { loadGeneratorStatus().then((status) => setGeneratorModel(status.ready ? status.model : `${status.model}（缺少 ${status.credential_env}）`)).catch((error: Error) => setGeneratorModel(error.message)) }, [])
  useEffect(() => { loadIntegrationsStatus().then(setIntegrations).catch(() => setIntegrations({ feishu: [], qq: [] })) }, [])
  useEffect(() => { loadRuntimeStatus().then(setRuntimeStatus).catch(() => setRuntimeStatus(null)) }, [])

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => setNodes((items) => applyNodeChanges(changes, items)), [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((items) => applyEdgeChanges(changes, items)), [])
  const onConnect = useCallback((connection: Connection) => setEdges((items) => addEdge({ ...connection, type: 'water' }, items)), [])
  const onNodesDelete = useCallback((deletedNodes: CanvasNode[]) => {
    const deletedIds = new Set(deletedNodes.map((node) => node.id))
    setEdges((items) => items.filter((edge) => !deletedIds.has(edge.source) && !deletedIds.has(edge.target)))
    setSelected((current) => current && deletedIds.has(current) ? null : current)
    setSelectedEdge(null)
  }, [])
  const onEdgesDelete = useCallback((deletedEdges: Edge[]) => {
    const deletedIds = new Set(deletedEdges.map((edge) => edge.id))
    setSelectedEdge((current) => current && deletedIds.has(current) ? null : current)
  }, [])
  const onSelectionChange = useCallback(({ nodes: selectedNodes, edges: selectedEdges }: { nodes: CanvasNode[]; edges: Edge[] }) => {
    setSelected(selectedNodes.length === 1 ? selectedNodes[0].id : null)
    setSelectedEdge(selectedNodes.length === 0 && selectedEdges.length === 1 ? selectedEdges[0].id : null)
  }, [])

  function deleteSelection() {
    const deletedNodeIds = new Set(nodes.filter((node) => node.selected).map((node) => node.id))
    const deletedEdgeIds = new Set(edges.filter((edge) => edge.selected).map((edge) => edge.id))
    setNodes((items) => items.filter((node) => !deletedNodeIds.has(node.id)))
    setEdges((items) => items.filter((edge) => !deletedEdgeIds.has(edge.id) && !deletedNodeIds.has(edge.source) && !deletedNodeIds.has(edge.target)))
    setSelected(null)
    setSelectedEdge(null)
    setMessage(`已删除 ${deletedNodeIds.size} 个节点和 ${deletedEdgeIds.size} 条连线`)
  }

  function onDrop(event: React.DragEvent) {
    event.preventDefault()
    const kind = event.dataTransfer.getData('application/openagent-node') as NodeKind
    if (!kind) return
    const catalog = nodeCatalog.find((item) => item.type === kind)!
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
    const id = `${kind}-${crypto.randomUUID().slice(0, 8)}`
    const defaults = { ...nodeDefaults[kind], ...(kind === 'webhook' ? { path: `/hooks/${workflowId}/${id}` } : {}) }
    setNodes((items) => [...items, {
      id, position, type: 'default', data: { kind, label: catalog.label, description: catalog.description, ...defaults },
      style: nodeStyle,
    }])
  }

  function updateSelected(field: string, value: unknown) {
    if (!selected) return
    setNodes((items) => items.map((node) => node.id === selected ? { ...node, data: { ...node.data, [field]: value, ...(field === 'description' ? { label: String(value) || nodeLabel(node.data.kind) } : {}) } } : node))
  }

  function updateSelectedJson(field: string, value: string) {
    try { updateSelected(field, JSON.parse(value)); setMessage(`${field} JSON 已更新`) }
    catch { setMessage(`${field} 不是有效 JSON，未保存`) }
  }

  function updateEdgeCondition(value: string) {
    if (!selectedEdge) return
    setEdges((items) => items.map((edge) => edge.id === selectedEdge ? { ...edge, label: value || undefined } : edge))
  }

  async function persist() {
    if (!workflow || !spec) return false
    setSaving(true); setMessage('正在保存工作流…')
    const payload: Workflow = {
      ...workflow,
      nodes: nodes.map((node) => { const { label: _label, kind: _kind, ...data } = node.data; return { id: node.id, type: node.data.kind, position: node.position, data } }),
      edges: edges.map((edge) => ({ source: edge.source, target: edge.target, ...(typeof edge.label === 'string' && edge.label ? { condition: edge.label } : {}) })),
    }
    try {
      const result = await saveWorkflow(payload, etag)
      setEtag(result.etag)
      setSpec({ ...spec, workflows: spec.workflows.map((item) => item.id === workflow.id ? result.workflow : item) })
      setMessage('工作流已保存')
      return true
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        try {
          const latest = await loadSpec()
          const serverWorkflow = latest.spec.workflows.find((item) => item.id === workflow.id)
          if (serverWorkflow && JSON.stringify(serverWorkflow) === JSON.stringify(payload)) {
            setSpec(latest.spec); setEtag(latest.etag); setMessage('服务端已包含当前画布，版本已同步')
            return true
          }
          if (serverWorkflow && JSON.stringify(serverWorkflow) === JSON.stringify(workflow)) {
            const retried = await saveWorkflow(payload, latest.etag)
            setEtag(retried.etag)
            setSpec({ ...latest.spec, workflows: latest.spec.workflows.map((item) => item.id === workflow.id ? retried.workflow : item) })
            setMessage('检测到其他配置更新，已同步版本并保存当前工作流')
            return true
          }
          if (serverWorkflow) {
            const view = toCanvas(serverWorkflow)
            setNodes(view.nodes); setEdges(view.edges)
          }
          setSpec(latest.spec); setEtag(latest.etag)
          setMessage('服务端工作流已发生并发修改，已载入最新版本；请确认后重新发送')
          return false
        } catch (refreshError) {
          setMessage(refreshError instanceof Error ? refreshError.message : '刷新工作流版本失败')
          return false
        }
      }
      setMessage(error instanceof Error ? error.message : '保存失败')
      return false
    }
    finally { setSaving(false) }
  }

  function updateEvaluationCases(cases: EvaluationCase[]) {
    if (!spec || !workflow) return
    setSpec({ ...spec, workflows: spec.workflows.map((item) => item.id === workflow.id ? { ...item, evaluation: { cases } } : item) })
  }

  function applyCompletedWorkflow(completed: Workflow, tag: string) {
    const view = toCanvas(completed)
    setNodes(view.nodes); setEdges(view.edges); setEtag(tag)
    setSpec((project) => project ? { ...project, workflows: project.workflows.map((item) => item.id === completed.id ? completed : item) } : project)
    setTimeout(() => fitView({ padding: 0.22 }), 40)
  }

  function followGeneration(started: { generation_id: string }, successMessage: string) {
    setGenerationId(started.generation_id)
    const source = new EventSource(`/api/generator/generations/${started.generation_id}/events`)
    const listen = (name: string, handler: (data: any) => void) => source.addEventListener(name, (event) => handler(JSON.parse((event as MessageEvent).data)))
    const stages: Record<string, string> = {
      understanding: 'OpenCode 正在理解问题和判断是否需要修改画布…',
      checking_runtime: '正在检查 Harness 验收运行时…',
      planning_layer: '正在规划下一层节点或分支…',
      preparing_cases: '正在准备完整验收用例…',
      validating_node: '正在检查新节点与连线的结构连通性…',
      probing_layer: '正在真实探测当前层节点…',
      full_evaluating: '正在完整验收当前工作流…',
      layer_failed: '当前层未通过，正在根据失败证据重建…',
      saving: '正在保存完整验收通过的工作流…',
    }
    listen('generation.stage', ({ stage, round }) => setMessage(`${stages[stage] || '正在优化工作流…'}${round ? `（第 ${round} 轮）` : ''}`))
    listen('generation.context_compacting', ({ before_chars }) => setMessage(`上下文较长（${before_chars} 字），正在提炼重点…`))
    listen('generation.context_compaction_retry', () => setMessage('首次上下文提炼返回空内容，正在使用同一 OpenCode compaction Agent 严格重试…'))
    listen('generation.context_compacted', ({ before_chars, after_chars, used }) => setMessage(used ? `上下文已从 ${before_chars} 字压缩至 ${after_chars} 字，正在继续生成…` : '上下文无需压缩，正在继续生成…'))
    listen('generation.context_compaction_failed', ({ before_chars }) => setMessage(`上下文提炼超时，已保留 ${before_chars} 字完整上下文继续生成…`))
    listen('generation.repairing', ({ message: repairMessage, node_id }) => setMessage(`${repairMessage}${node_id ? `（节点 ${node_id}）` : ''}`))
    const applyStreamingWorkflow = (workflow: Workflow, reverted = false) => {
      if (!workflow || workflow.id !== workflowId) return
      const view = toCanvas(workflow)
      setNodes(view.nodes); setEdges(view.edges)
      setSpec((project) => project ? { ...project, workflows: project.workflows.map((item) => item.id === workflow.id ? workflow : item) } : project)
      setMessage(reverted ? '本步探测未通过，已恢复上一个稳定版本' : `正在实时渲染：${workflow.nodes.length} 个节点、${workflow.edges.length} 条连线…`)
      setTimeout(() => fitView({ padding: 0.22 }), 40)
    }
    listen('workflow.preview', ({ workflow, reverted }) => applyStreamingWorkflow(workflow, reverted))
    listen('workflow.updated', ({ workflow }) => applyStreamingWorkflow(workflow))
    listen('chat.assistant.delta', ({ text }) => setChatMessages((items) => {
      const last = items[items.length - 1]
      if (last?.role === 'assistant') return [...items.slice(0, -1), { ...last, content: last.content + text }]
      return [...items, { role: 'assistant', content: text }]
    }))
    listen('chat.completed', ({ options }) => {
      if (Array.isArray(options) && options.length === 3) {
        setChatMessages((items) => {
          const last = items[items.length - 1]
          return last?.role === 'assistant' ? [...items.slice(0, -1), { ...last, options }] : items
        })
      }
      setGenerationId(null); setMessage('OpenCode 已回复'); source.close()
    })
    listen('generation.completed', ({ workflow: completed, etag: tag, assistant_message }) => {
      applyCompletedWorkflow(completed, tag)
      if (assistant_message) setChatMessages((items) => [...items, { role: 'assistant', content: assistant_message }])
      setGenerationId(null); setMessage(successMessage); source.close()
    })
    const finishError = async (text: string) => {
      source.close()
      setMessage(`${text}；正在恢复服务端稳定版本…`)
      setChatMessages((items) => [...items, { role: 'system', content: text }])
      try {
        const latest = await loadSpec()
        const stable = latest.spec.workflows.find((item) => item.id === workflowId)
        setSpec(latest.spec); setEtag(latest.etag)
        if (stable) {
          const view = toCanvas(stable)
          setNodes(view.nodes); setEdges(view.edges)
          setTimeout(() => fitView({ padding: 0.22 }), 40)
        }
        setMessage(text)
      } catch (error) {
        const detail = error instanceof Error ? error.message : '无法刷新项目配置'
        setMessage(`${text}；恢复服务端版本失败：${detail}`)
      } finally {
        setGenerationId(null)
      }
    }
    listen('generation.failed', ({ message: text }) => { void finishError(`优化失败：${text}`) })
    listen('generation.conflict', ({ message: text }) => { void finishError(text) })
    listen('generation.cancelled', () => { void finishError('已停止本轮优化，原工作流未改变') })
    source.onerror = () => { if (source.readyState === EventSource.CLOSED) setGenerationId(null) }
  }

  async function generateFromChat(selectedOption?: string) {
    const text = (selectedOption ?? chatInput).trim()
    if (!text || !workflowId || generationId) return
    try {
      setMessage('正在同步完整画布给 OpenCode…')
      if (!await persist()) return
      setChatInput('')
      setChatMessages((items) => [...items.map((item) => item.options ? { ...item, options: undefined } : item), { role: 'user', content: text }])
      setMessage(`OpenCode 正在读取 ${nodes.length} 个节点和 ${edges.length} 条连线…`)
      const started = await sendGeneratorMessage(workflowId, text)
      followGeneration(started, '工作流已通过验收并自动采用最佳方案')
    } catch (error) {
      const text = error instanceof Error ? error.message : '无法启动生成器'
      setMessage(text); setChatMessages((items) => [...items, { role: 'system', content: text }])
    }
  }

  async function optimizeCurrent() {
    if (!workflowId || generationId) return
    try {
      if (!await persist()) return
      setRightTab('chat'); setMessage('正在启动最佳工作流选择…')
      followGeneration(await optimizeWorkflow(workflowId), '已采用通过验收的最佳工作流')
    } catch (error) { setMessage(error instanceof Error ? error.message : '无法启动优化') }
  }

  async function stopGeneration() {
    if (!generationId) return
    await cancelGeneration(generationId)
    setMessage('正在停止 OpenCode 生成器…')
  }

  function followRun(started: WorkflowRun) {
    setRun(started); setRightTab('run'); setRunEvents([])
    const source = new EventSource(`/api/workflow-runs/${started.id}/events`)
    const names = ['run.started', 'node.status', 'node.harness_task', 'node.progress', 'node.approval_required', 'node.approval_resolved', 'run.completed', 'run.failed', 'run.cancelled']
    names.forEach((name) => source.addEventListener(name, (event) => {
      const data = JSON.parse((event as MessageEvent).data)
      setRunEvents((items) => [...items.slice(-99), `${name} · ${data.node_id || data.message || data.status || ''}`])
      if (data.run) setRun(data.run)
      else setRun((current) => current && data.node_id && data.state ? { ...current, node_states: { ...current.node_states, [data.node_id]: data.state } } : current)
      if (name === 'run.completed' || name === 'run.failed' || name === 'run.cancelled') source.close()
    }))
  }

  async function executeWorkflow() {
    if (!workflowId || saving || run && ['queued', 'running'].includes(run.status)) return
    try {
      if (!await persist()) return
      const started = await startWorkflowRun(workflowId, runInput)
      setMessage('工作流已提交给 Harness 执行器')
      followRun(started)
    } catch (error) { setMessage(error instanceof Error ? error.message : '无法启动工作流') }
  }

  async function stopRun() {
    if (!run) return
    setRun(await cancelWorkflowRun(run.id)); setMessage('正在取消工作流及 Harness 任务…')
  }

  async function decide(nodeId: string, approved: boolean) {
    if (!run) return
    try { setRun(await resolveApproval(run.id, nodeId, approved)); setMessage(approved ? '审批已通过' : '审批已拒绝') }
    catch (error) { setMessage(error instanceof Error ? error.message : '审批失败') }
  }

  return <div className="studio-shell">
    <video className="workspace-video" autoPlay loop muted playsInline preload="metadata" aria-hidden="true">
      <source src="https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260315_073750_51473149-4350-4920-ae24-c8214286f323.mp4" type="video/mp4"/>
    </video>
    <header className="topbar structural-glass">
      <div className="brand"><span className="logo">OA</span><div><strong>OpenAgent Studio</strong><small>{spec?.name ?? '智能体工作流'}</small></div></div>
      <div className="top-actions">
        {integrations.feishu.map((item) => <span key={`feishu-${item.id}`} className={`integration-badge ${item.ready ? 'ready' : ''}`} title={item.ready ? `飞书 → ${item.workflow_id}` : `缺少 ${item.missing_env.join(', ')}`}>飞书 {item.ready ? '已连接' : '待配置'}</span>)}
        {integrations.qq.map((item) => <span key={`qq-${item.id}`} className={`integration-badge ${item.ready ? 'ready' : ''}`} title={item.ready ? `QQ → ${item.workflow_id}` : `缺少 ${item.missing_env.join(', ')}`}>QQ {item.ready ? '已连接' : '待配置'}</span>)}
        <select value={workflowId} onChange={(event) => spec && openWorkflow(spec, event.target.value)}>
          {spec?.workflows.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button className="ghost" onClick={() => fitView({ padding: 0.22 })}>适应画布</button>
        <button className="ghost" disabled={!!generationId || !workflow} onClick={optimizeCurrent}>优化当前工作流</button>
        <button className="opencode-launch" onClick={() => setRightTab('chat')}>✦ OpenCode 创建</button>
        <button className="primary" disabled={saving || !workflow} onClick={persist}>{saving ? '保存中…' : '保存工作流'}</button>
        {run && ['queued', 'running'].includes(run.status) ? <button className="stop-run" onClick={stopRun}>■ 停止</button> : <button className="run" disabled={!workflow} onClick={executeWorkflow}>▶ 运行</button>}
      </div>
    </header>

    <aside className="node-library structural-glass">
      <div className="panel-title"><strong>节点库</strong><span>拖入画布开始编排</span></div>
      <div className="library-search"><input value={libraryQuery} onChange={(event) => setLibraryQuery(event.target.value)} placeholder="搜索节点名称、类型或能力…" aria-label="搜索节点"/>{libraryQuery && <button onClick={() => setLibraryQuery('')} aria-label="清空节点搜索">×</button>}</div>
      <div className="library-list">{Array.from(new Set(visibleCatalog.map((item) => item.category))).map((category) => <details className="library-group" key={category} open><summary>{category}<span>{visibleCatalog.filter((item) => item.category === category).length}</span></summary>{visibleCatalog.filter((item) => item.category === category).map((item) => <div key={item.type} className="library-item liquid-glass" draggable onDragStart={(event) => { event.dataTransfer.setData('application/openagent-node', item.type); event.dataTransfer.effectAllowed = 'move' }}>
        <span className={`node-icon ${item.type}`}>{item.icon}</span><div><strong>{item.label}</strong><small>{item.description}</small></div>
      </div>)}</details>)}{visibleCatalog.length === 0 && <div className="library-empty">没有匹配的节点</div>}</div>
    </aside>

    <main className="canvas-area" onDrop={onDrop} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }}>
      <ReactFlow nodes={nodes} edges={edges} edgeTypes={edgeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onNodesDelete={onNodesDelete} onEdgesDelete={onEdgesDelete} onSelectionChange={onSelectionChange} onConnect={onConnect} onNodeClick={(_, node) => { setSelected(node.id); setSelectedEdge(null); setRightTab('node') }} onEdgeClick={(_, edge) => { setSelectedEdge(edge.id); setSelected(null); setRightTab('edge') }} onPaneClick={() => { setSelected(null); setSelectedEdge(null) }} selectionOnDrag selectionMode={SelectionMode.Partial} panOnDrag={[1, 2]} deleteKeyCode={['Backspace', 'Delete']} fitView>
        <Background color="#3f3f3f" gap={22} size={1}/><Controls/><MiniMap pannable zoomable nodeColor="#bdbdbd" maskColor="#090909b8"/>
        <Panel position="top-left" className="canvas-hint liquid-glass">拖拽节点并连接端点 · 空白处框选 · Delete 删除 · 中/右键平移</Panel>
        {selectionCount > 0 && <Panel position="top-center" className="selection-toolbar liquid-glass nodrag nopan"><span>已选 {selectedNodeCount} 个节点{selectedEdgeCount > 0 ? `、${selectedEdgeCount} 条连线` : ''}</span><button type="button" onClick={deleteSelection} aria-label="删除选中元素">删除所选</button></Panel>}
      </ReactFlow>
    </main>

    <aside className={`inspector structural-glass ${rightTab === 'chat' ? 'chat-mode' : ''}`}>
      <div className="inspector-tabs"><button className={rightTab === 'chat' ? 'active' : ''} onClick={() => setRightTab('chat')}>AI 创建</button><button className={rightTab === 'node' || rightTab === 'edge' ? 'active' : ''} onClick={() => setRightTab(selectedEdge ? 'edge' : 'node')}>设置</button><button className={rightTab === 'evaluation' ? 'active' : ''} onClick={() => setRightTab('evaluation')}>验收标准</button><button className={rightTab === 'run' ? 'active' : ''} onClick={() => setRightTab('run')}>运行</button></div>
      {rightTab === 'chat' ? <div className="chat-panel">
        <div className="generator-title"><span className="ai-orb">✦</span><div><strong>OpenCode 创作助手</strong><small>真实模型：{generatorModel}</small></div></div>
        <div className="chat-messages">
          {chatMessages.length === 0 && <div className="chat-empty"><p>你可以这样说：</p><button onClick={() => setChatInput('创建一个代码审查流程，先分析代码，再人工审批，最后运行测试')}>创建代码审查流程</button><button onClick={() => setChatInput('在当前流程的验证前增加一个人工审批节点')}>增量修改当前流程</button></div>}
          {chatMessages.map((item, index) => <div key={index} className={`chat-message ${item.role}`}><span>{item.role === 'user' ? '你' : item.role === 'assistant' ? 'AI' : '!'}</span><div className="chat-message-body"><p>{item.content}</p>{item.options?.length === 3 && index === chatMessages.length - 1 && <div className="chat-options">{item.options.map((option) => <button key={option} disabled={!!generationId} onClick={() => void generateFromChat(option)}>{option}</button>)}<small>或在下方输入你的自定义答案</small></div>}</div></div>)}
          {generationId && <div className="thinking"><i/><i/><i/><span>OpenCode 正在思考…</span></div>}
        </div>
        <div className="chat-composer"><textarea autoFocus value={chatInput} disabled={!!generationId} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void generateFromChat() } }} placeholder={chatMessages.at(-1)?.options?.length === 3 ? '自定义输入（第 4 个选项）…' : '可以询问当前工作流，也可以要求创建、修改或优化节点…'}/>
          {generationId ? <button className="stop" onClick={stopGeneration}>停止生成</button> : <button className="send" onClick={() => void generateFromChat()} disabled={!chatInput.trim()}>发送并构建工作流</button>}
        </div>
      </div> : rightTab === 'evaluation' ? <div className="run-panel evaluation-panel">
        <div className="panel-title"><strong>验收标准</strong><span>OpenCode 会保留旧用例，并为改动追加最多 3 个</span></div>
        {(workflow?.evaluation?.cases || []).map((item, index) => <fieldset className="advanced-fields" key={item.id}>
          <legend>{item.name || item.id}</legend>
          <label className="check-row"><input type="checkbox" checked={item.enabled} onChange={(event) => updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, enabled: event.target.checked } : value))}/>启用</label>
          <label>名称<input value={item.name} onChange={(event) => updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, name: event.target.value } : value))}/></label>
          <label>输入 JSON<textarea defaultValue={JSON.stringify(item.input, null, 2)} onBlur={(event) => { try { const input = JSON.parse(event.target.value); updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, input } : value)) } catch { setMessage('验收输入不是有效 JSON') } }}/></label>
          <label>确定性断言 JSON<textarea defaultValue={JSON.stringify(item.assertions, null, 2)} onBlur={(event) => { try { const assertions = JSON.parse(event.target.value); updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, assertions } : value)) } catch { setMessage('断言不是有效 JSON') } }}/></label>
          <label>语义标准（每行一条）<textarea value={item.semantic_criteria.join('\n')} onChange={(event) => updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, semantic_criteria: event.target.value.split('\n').filter(Boolean) } : value))}/></label>
          <label>审批决策 JSON<textarea defaultValue={JSON.stringify(item.approvals, null, 2)} onBlur={(event) => { try { const approvals = JSON.parse(event.target.value); updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, approvals } : value)) } catch { setMessage('审批决策不是有效 JSON') } }}/></label>
          <label>单用例超时（秒）<input type="number" min="1" max="1800" value={item.timeout_seconds} onChange={(event) => updateEvaluationCases((workflow?.evaluation?.cases || []).map((value, i) => i === index ? { ...value, timeout_seconds: Number(event.target.value) } : value))}/></label>
        </fieldset>)}
        {!workflow?.evaluation?.cases?.length && <div className="empty-state"><p>首次使用 AI 创建或优化时<br/>会自动生成 3 个验收用例</p></div>}
        <button className="run-wide" disabled={!!generationId} onClick={optimizeCurrent}>优化当前工作流</button>
      </div> : rightTab === 'run' ? <div className="run-panel">
        <div className="panel-title"><strong>Harness 工作流</strong><span>{run ? `运行 ${run.id.slice(0, 8)} · ${run.status}` : '输入任务后开始执行'}</span></div>
        <label>工作流输入<textarea value={runInput} onChange={(event) => setRunInput(event.target.value)} disabled={!!run && ['queued', 'running'].includes(run.status)} placeholder="输入要交给工作流处理的任务…"/></label>
        {!run || !['queued', 'running'].includes(run.status) ? <button className="run-wide" onClick={executeWorkflow}>▶ 开始运行</button> : <button className="stop-wide" onClick={stopRun}>■ 取消运行</button>}
        {run && <div className="run-summary"><strong>{run.status}</strong>{run.error && <p>{run.error}</p>}{Object.entries(run.node_states).map(([id, state]) => <div className={`node-run ${state.status}`} key={id}><span>{id}</span><em>{state.status}</em>{state.status === 'waiting' && <span className="approval-actions"><button onClick={() => decide(id, true)}>通过</button><button onClick={() => decide(id, false)}>拒绝</button></span>}</div>)}</div>}
        <div className="run-events">{runEvents.map((item, index) => <code key={index}>{item}</code>)}</div>
      </div> : rightTab === 'edge' ? <>
        <div className="panel-title"><strong>连线设置</strong><span>控制条件分支</span></div>
        {currentEdge ? <div className="form-stack">
          <label>源节点<input value={currentEdge.source} disabled/></label>
          <label>目标节点<input value={currentEdge.target} disabled/></label>
          <label>执行条件<input value={typeof currentEdge.label === 'string' ? currentEdge.label : ''} onChange={(event) => updateEdgeCondition(event.target.value)} placeholder="true / false / latest.status == &quot;completed&quot;"/></label>
          <small>留空表示始终执行；条件节点常用 true 和 false 分流。</small>
          <button className="danger" onClick={() => { setEdges((items) => items.filter((edge) => edge.id !== currentEdge.id)); setSelectedEdge(null) }}>删除连线</button>
        </div> : <div className="empty-state"><span>◇</span><p>点击画布中的连线<br/>配置分支条件</p></div>}
      </> : <>
        <div className="panel-title"><strong>节点设置</strong><span>{selectedNode ? nodeLabel(selectedNode.data.kind) : '请选择一个节点'}</span></div>
        {selectedNode ? <div className="form-stack">
          <label>节点名称<input value={selectedNode.data.description} onChange={(event) => updateSelected('description', event.target.value)}/></label>
          <label>节点类型<input value={nodeLabel(selectedNode.data.kind)} disabled/></label>
          {harnessNodeKinds.includes(selectedNode.data.kind) && <label>Harness 智能体<select value={selectedNode.data.agent_id || ''} onChange={(event) => updateSelected('agent_id', event.target.value)}><option value="">{selectedNode.data.kind === 'validator' ? '复用上一步 Harness 验证' : '请选择'}</option>{spec?.harness.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>}
          {harnessNodeKinds.includes(selectedNode.data.kind) && <><label>任务标题<input value={String(selectedNode.data.title || '')} onChange={(event) => updateSelected('title', event.target.value)} placeholder="用于 Harness 任务列表"/></label><label>任务提示词<textarea value={String(selectedNode.data.prompt || '')} onChange={(event) => updateSelected('prompt', event.target.value)} placeholder="写明角色、目标、输入、约束和输出；支持 {{input}}、{{latest}}"/></label></>}
          {harnessNodeKinds.includes(selectedNode.data.kind) && selectedHarness && <label>相对工作目录<input value={String(selectedNode.data.relative_path || '.')} onChange={(event) => updateSelected('relative_path', event.target.value)} placeholder="."/></label>}
          {selectedNode.data.kind === 'prompt' && <label>提示词模板<textarea value={String(selectedNode.data.template || '')} onChange={(event) => updateSelected('template', event.target.value)} placeholder="支持 {{input}} 和 {{latest}}"/></label>}
          {selectedNode.data.kind === 'webhook' && <><label>Webhook 路径<input value={String(selectedNode.data.path || '')} onChange={(event) => updateSelected('path', event.target.value)} placeholder="/hooks/product-selection"/></label><label>请求方法<select value={String(selectedNode.data.method || 'POST')} onChange={(event) => updateSelected('method', event.target.value)}>{['GET', 'POST', 'PUT', 'PATCH'].map((method) => <option key={method}>{method}</option>)}</select></label></>}
          {selectedNode.data.kind === 'schedule' && <><label>Cron 表达式<input value={String(selectedNode.data.cron || '')} onChange={(event) => updateSelected('cron', event.target.value)} placeholder="0 9 * * *"/></label><label>时区<input value={String(selectedNode.data.timezone || 'Asia/Shanghai')} onChange={(event) => updateSelected('timezone', event.target.value)}/></label></>}
          {selectedNode.data.kind === 'knowledge_retrieval' && <><label>查询模板<textarea value={String(selectedNode.data.query || '{{latest}}')} onChange={(event) => updateSelected('query', event.target.value)}/></label><label>召回数量<input type="number" min="1" max="20" value={String(selectedNode.data.top_k || 3)} onChange={(event) => updateSelected('top_k', Number(event.target.value))}/></label><label>知识文档 JSON<textarea key={`${selectedNode.id}-documents`} defaultValue={JSON.stringify(selectedNode.data.documents || [], null, 2)} onBlur={(event) => updateSelectedJson('documents', event.target.value)} placeholder='[{"id":"doc-1","content":"..."}]'/></label></>}
          {selectedNode.data.kind === 'http_request' && <><label>URL<input value={String(selectedNode.data.url || '')} onChange={(event) => updateSelected('url', event.target.value)} placeholder="https://api.example.com/items"/></label><label>HTTP 方法<select value={String(selectedNode.data.method || 'GET')} onChange={(event) => updateSelected('method', event.target.value)}>{['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method}>{method}</option>)}</select></label><label>请求头 JSON<textarea key={`${selectedNode.id}-headers`} defaultValue={JSON.stringify(selectedNode.data.headers || {}, null, 2)} onBlur={(event) => updateSelectedJson('headers', event.target.value)}/></label><label>请求体 JSON/模板<textarea key={`${selectedNode.id}-body`} defaultValue={typeof selectedNode.data.body === 'string' ? selectedNode.data.body : JSON.stringify(selectedNode.data.body || {}, null, 2)} onBlur={(event) => { try { updateSelected('body', JSON.parse(event.target.value)) } catch { updateSelected('body', event.target.value) } }}/></label><label>超时（秒）<input type="number" min="1" max="120" value={String(selectedNode.data.timeout_seconds || 30)} onChange={(event) => updateSelected('timeout_seconds', Number(event.target.value))}/></label><label className="check-row"><input type="checkbox" checked={selectedNode.data.fail_on_error !== false} onChange={(event) => updateSelected('fail_on_error', event.target.checked)}/>4xx/5xx 时失败</label><label className="check-row"><input type="checkbox" checked={selectedNode.data.allow_private === true} onChange={(event) => updateSelected('allow_private', event.target.checked)}/>允许受信任的内网 HTTP</label></>}
          {selectedNode.data.kind === 'variable_set' && <label>变量对象 JSON<textarea key={`${selectedNode.id}-variables`} defaultValue={JSON.stringify(selectedNode.data.variables || {}, null, 2)} onBlur={(event) => updateSelectedJson('variables', event.target.value)} placeholder='{"category":"{{input}}"}'/></label>}
          {selectedNode.data.kind === 'transform' && <><label>转换操作<select value={String(selectedNode.data.operation || 'json_stringify')} onChange={(event) => updateSelected('operation', event.target.value)}>{['json_parse', 'json_stringify', 'extract', 'pick', 'flatten'].map((value) => <option key={value}>{value}</option>)}</select></label>{selectedNode.data.operation === 'extract' && <label>数据路径<input value={String(selectedNode.data.path || '')} onChange={(event) => updateSelected('path', event.target.value)} placeholder="body.items.0"/></label>}{selectedNode.data.operation === 'pick' && <label>字段 JSON<textarea key={`${selectedNode.id}-fields`} defaultValue={JSON.stringify(selectedNode.data.fields || [], null, 2)} onBlur={(event) => updateSelectedJson('fields', event.target.value)}/></label>}</>}
          {selectedNode.data.kind === 'merge' && <><label>合并模式<select value={String(selectedNode.data.mode || 'array')} onChange={(event) => updateSelected('mode', event.target.value)}>{['array', 'object', 'concat'].map((value) => <option key={value}>{value}</option>)}</select></label>{selectedNode.data.mode === 'concat' && <label>分隔符<input value={String(selectedNode.data.separator || '\n')} onChange={(event) => updateSelected('separator', event.target.value)}/></label>}</>}
          {selectedNode.data.kind === 'condition' && <label>条件表达式<input value={String(selectedNode.data.expression || '')} onChange={(event) => updateSelected('expression', event.target.value)} placeholder={'latest.status == "completed"'}/></label>}
          {selectedNode.data.kind === 'switch' && <><label>分支规则 JSON<textarea key={`${selectedNode.id}-cases`} defaultValue={JSON.stringify(selectedNode.data.cases || [], null, 2)} onBlur={(event) => updateSelectedJson('cases', event.target.value)} placeholder='[{"value":"hot","expression":"latest.score == 5"}]'/></label><label>默认分支<input value={String(selectedNode.data.default_case || 'default')} onChange={(event) => updateSelected('default_case', event.target.value)}/></label></>}
          {selectedNode.data.kind === 'validator' && !selectedNode.data.agent_id && <label>验证表达式<input value={String(selectedNode.data.expression || '')} onChange={(event) => updateSelected('expression', event.target.value)} placeholder="留空则检查上一步 Harness task"/></label>}
          {(selectedNode.data.kind === 'loop' || selectedNode.data.kind === 'iteration') && <><label>迭代次数<input type="number" min="1" max="100" value={String(selectedNode.data.iterations || 1)} onChange={(event) => updateSelected('iterations', Number(event.target.value))}/></label><label>迭代模板<textarea value={String(selectedNode.data.template || '{{latest}}')} onChange={(event) => updateSelected('template', event.target.value)} placeholder="第 {{index}} 次：{{latest}}"/></label></>}
          {selectedNode.data.kind === 'approval' && <label>审批说明<textarea value={String(selectedNode.data.instructions || '')} onChange={(event) => updateSelected('instructions', event.target.value)}/></label>}
          {selectedNode.data.kind === 'subworkflow' && <><label>选择工作流<select value={String(selectedNode.data.workflow_id || '')} onChange={(event) => updateSelected('workflow_id', event.target.value)}><option value="">请选择</option>{spec?.workflows.filter((item) => item.id !== workflowId).map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>输入模板<textarea value={String(selectedNode.data.input_template || '{{latest}}')} onChange={(event) => updateSelected('input_template', event.target.value)}/></label></>}
          {selectedNode.data.kind === 'delay' && <label>等待秒数<input type="number" min="0" max="300" step="0.1" value={String(selectedNode.data.seconds || 1)} onChange={(event) => updateSelected('seconds', Number(event.target.value))}/></label>}
          {selectedNode.data.kind === 'output' && <label>输出模板<textarea value={String(selectedNode.data.template || '')} onChange={(event) => updateSelected('template', event.target.value)} placeholder="留空则透传上一步结果"/></label>}
          {!['manual_trigger', 'webhook', 'schedule', 'approval'].includes(selectedNode.data.kind) && <fieldset className="advanced-fields"><legend>失败与重试</legend><label>重试次数<input type="number" min="0" max="5" value={String(selectedNode.data.retry_count || 0)} onChange={(event) => updateSelected('retry_count', Number(event.target.value))}/></label><label>重试间隔（秒）<input type="number" min="0" max="30" value={String(selectedNode.data.retry_delay_seconds || 1)} onChange={(event) => updateSelected('retry_delay_seconds', Number(event.target.value))}/></label><label>失败策略<select value={String(selectedNode.data.on_error || 'fail')} onChange={(event) => updateSelected('on_error', event.target.value)}><option value="fail">终止工作流</option><option value="continue">使用兜底值继续</option></select></label>{selectedNode.data.on_error === 'continue' && <label>兜底值 JSON/文本<textarea value={typeof selectedNode.data.fallback_value === 'string' ? selectedNode.data.fallback_value : JSON.stringify(selectedNode.data.fallback_value ?? '')} onChange={(event) => { try { updateSelected('fallback_value', JSON.parse(event.target.value)) } catch { updateSelected('fallback_value', event.target.value) } }}/></label>}</fieldset>}
          <label>节点编号<input value={selectedNode.id} disabled/></label>
          <button className="danger" onClick={() => { setNodes((items) => items.filter((node) => node.id !== selectedNode.id)); setEdges((items) => items.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id)); setSelected(null) }}>删除节点</button>
        </div> : <div className="empty-state"><span>◇</span><p>点击画布中的节点<br/>在这里编辑详细参数</p></div>}
      </>}
    </aside>

    <footer className="console"><span className="status-dot"/><strong>运行控制台</strong><span>{message}</span><span className="console-meta">{nodes.length} 个节点 · {edges.length} 条连线</span>{runtimeStatus && !runtimeStatus.running && <span className="runtime-warning">Harness 未就绪：需要注册/setup/修正配置</span>}</footer>
  </div>
}

export default function App() { return <ReactFlowProvider><StudioCanvas/></ReactFlowProvider> }
