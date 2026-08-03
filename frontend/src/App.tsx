import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Background, BaseEdge, Controls, EdgeLabelRenderer, MiniMap, Panel, ReactFlow, ReactFlowProvider,
  addEdge, applyEdgeChanges, applyNodeChanges, getBezierPath, useReactFlow,
  type Connection, type Edge, type EdgeChange, type EdgeProps, type EdgeTypes, type Node, type NodeChange,
} from '@xyflow/react'
import { cancelGeneration, cancelWorkflowRun, loadGeneratorMessages, loadGeneratorStatus, loadSpec, resolveApproval, saveWorkflow, sendGeneratorMessage, startWorkflowRun } from './api'
import type { NodeKind, ProjectSpec, Workflow, WorkflowRun } from './types'

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
type ChatMessage = { role: 'user' | 'assistant' | 'system'; content: string }

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
  const [rightTab, setRightTab] = useState<'chat' | 'node' | 'edge' | 'run'>('chat')
  const [chatInput, setChatInput] = useState('')
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [generationId, setGenerationId] = useState<string | null>(null)
  const [chatDialogOpen, setChatDialogOpen] = useState(true)
  const [generatorModel, setGeneratorModel] = useState('正在读取模型…')
  const [run, setRun] = useState<WorkflowRun | null>(null)
  const [runInput, setRunInput] = useState('')
  const [runEvents, setRunEvents] = useState<string[]>([])
  const [libraryQuery, setLibraryQuery] = useState('')
  const { screenToFlowPosition, fitView } = useReactFlow()

  const workflow = useMemo(() => spec?.workflows.find((item) => item.id === workflowId), [spec, workflowId])
  const selectedNode = nodes.find((item) => item.id === selected)
  const selectedHarness = spec?.harness.find((item) => item.id === selectedNode?.data.agent_id)
  const currentEdge = edges.find((item) => item.id === selectedEdge)
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

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => setNodes((items) => applyNodeChanges(changes, items)), [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((items) => applyEdgeChanges(changes, items)), [])
  const onConnect = useCallback((connection: Connection) => setEdges((items) => addEdge({ ...connection, type: 'water' }, items)), [])

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
    } catch (error) { setMessage(error instanceof Error ? error.message : '保存失败'); return false }
    finally { setSaving(false) }
  }

  async function generateFromChat() {
    const text = chatInput.trim()
    if (!text || !workflowId || generationId) return
    setChatInput('')
    setChatMessages((items) => [...items, { role: 'user', content: text }])
    setMessage('OpenCode 正在理解需求…')
    try {
      const started = await sendGeneratorMessage(workflowId, text)
      setGenerationId(started.generation_id)
      const source = new EventSource(`/api/generator/generations/${started.generation_id}/events`)
      const listen = (name: string, handler: (data: any) => void) => source.addEventListener(name, (event) => handler(JSON.parse((event as MessageEvent).data)))
      listen('chat.assistant.delta', (data) => setChatMessages((items) => {
        const last = items[items.length - 1]
        if (last?.role === 'assistant') return [...items.slice(0, -1), { ...last, content: last.content + data.text }]
        return [...items, { role: 'assistant', content: data.text }]
      }))
      listen('workflow.node.added', ({ node }) => {
        setNodes((items) => items.some((item) => item.id === node.id) ? items : [...items, {
          id: node.id, type: 'default', position: node.position,
          data: { ...node.data, kind: node.type, label: node.data.description || nodeLabel(node.type), description: node.data.description || '' },
          style: { ...nodeStyle, background: 'rgba(255,255,255,.1)', boxShadow: '0 12px 32px rgba(0,0,0,.42), inset 0 1px 1px rgba(255,255,255,.2)' },
        }])
        setMessage(`已生成节点：${node.data.description || node.id}`)
      })
      listen('workflow.node.updated', ({ node }) => setNodes((items) => items.map((item) => item.id === node.id ? { ...item, data: { ...item.data, ...node.data, label: node.data.description || item.data.label } } : item)))
      listen('workflow.node.deleted', ({ node_id }) => { setNodes((items) => items.filter((item) => item.id !== node_id)); setEdges((items) => items.filter((edge) => edge.source !== node_id && edge.target !== node_id)) })
      listen('workflow.edge.added', ({ edge }) => setEdges((items) => items.some((item) => item.source === edge.source && item.target === edge.target) ? items : addEdge({ ...edge, id: `ai-${crypto.randomUUID()}`, type: 'water' }, items)))
      listen('workflow.edge.deleted', ({ source: from, target }) => setEdges((items) => items.filter((edge) => !(edge.source === from && edge.target === target))))
      listen('operation.rejected', ({ message: text }) => setChatMessages((items) => [...items, { role: 'system', content: `操作未应用：${text}` }]))
      listen('generation.completed', ({ workflow: completed, etag: tag }) => {
        setEtag(tag); setGenerationId(null); setMessage('工作流已由 OpenCode 生成并保存'); source.close()
        setSpec((project) => project ? { ...project, workflows: project.workflows.map((item) => item.id === completed.id ? completed : item) } : project)
      })
      const finishError = (text: string) => { setGenerationId(null); setMessage(text); setChatMessages((items) => [...items, { role: 'system', content: text }]); source.close() }
      listen('generation.failed', ({ message: text }) => finishError(`生成失败：${text}`))
      listen('generation.conflict', ({ message: text }) => finishError(text))
      listen('generation.cancelled', () => finishError('已停止本轮生成'))
      source.onerror = () => { if (source.readyState === EventSource.CLOSED) setGenerationId(null) }
    } catch (error) {
      const text = error instanceof Error ? error.message : '无法启动生成器'
      setMessage(text); setChatMessages((items) => [...items, { role: 'system', content: text }])
    }
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
    <header className="topbar liquid-glass-strong">
      <div className="brand"><span className="logo">OA</span><div><strong>OpenAgent Studio</strong><small>{spec?.name ?? '智能体工作流'}</small></div></div>
      <div className="top-actions">
        <select value={workflowId} onChange={(event) => spec && openWorkflow(spec, event.target.value)}>
          {spec?.workflows.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button className="ghost" onClick={() => fitView({ padding: 0.22 })}>适应画布</button>
        <button className="opencode-launch" onClick={() => { setChatDialogOpen(true); setRightTab('chat') }}>✦ OpenCode 创建</button>
        <button className="primary" disabled={saving || !workflow} onClick={persist}>{saving ? '保存中…' : '保存工作流'}</button>
        {run && ['queued', 'running'].includes(run.status) ? <button className="stop-run" onClick={stopRun}>■ 停止</button> : <button className="run" disabled={!workflow} onClick={executeWorkflow}>▶ 运行</button>}
      </div>
    </header>

    <aside className="node-library liquid-glass-strong">
      <div className="panel-title"><strong>节点库</strong><span>拖入画布开始编排</span></div>
      <div className="library-search"><input value={libraryQuery} onChange={(event) => setLibraryQuery(event.target.value)} placeholder="搜索节点名称、类型或能力…" aria-label="搜索节点"/>{libraryQuery && <button onClick={() => setLibraryQuery('')} aria-label="清空节点搜索">×</button>}</div>
      <div className="library-list">{Array.from(new Set(visibleCatalog.map((item) => item.category))).map((category) => <details className="library-group" key={category} open><summary>{category}<span>{visibleCatalog.filter((item) => item.category === category).length}</span></summary>{visibleCatalog.filter((item) => item.category === category).map((item) => <div key={item.type} className="library-item liquid-glass" draggable onDragStart={(event) => { event.dataTransfer.setData('application/openagent-node', item.type); event.dataTransfer.effectAllowed = 'move' }}>
        <span className={`node-icon ${item.type}`}>{item.icon}</span><div><strong>{item.label}</strong><small>{item.description}</small></div>
      </div>)}</details>)}{visibleCatalog.length === 0 && <div className="library-empty">没有匹配的节点</div>}</div>
    </aside>

    <main className="canvas-area" onDrop={onDrop} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }}>
      <ReactFlow nodes={nodes} edges={edges} edgeTypes={edgeTypes} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={(_, node) => { setSelected(node.id); setSelectedEdge(null); setRightTab('node') }} onEdgeClick={(_, edge) => { setSelectedEdge(edge.id); setSelected(null); setRightTab('edge') }} onPaneClick={() => { setSelected(null); setSelectedEdge(null) }} fitView>
        <Background color="#3f3f3f" gap={22} size={1}/><Controls/><MiniMap pannable zoomable nodeColor="#bdbdbd" maskColor="#090909b8"/>
        <Panel position="top-left" className="canvas-hint liquid-glass">拖拽节点并连接端点，构建智能体执行流程</Panel>
      </ReactFlow>
    </main>

    <aside className="inspector liquid-glass-strong">
      <div className="inspector-tabs"><button className={rightTab === 'chat' ? 'active' : ''} onClick={() => { setRightTab('chat'); setChatDialogOpen(true) }}>AI 创建</button><button className={rightTab === 'node' || rightTab === 'edge' ? 'active' : ''} onClick={() => setRightTab(selectedEdge ? 'edge' : 'node')}>设置</button><button className={rightTab === 'run' ? 'active' : ''} onClick={() => setRightTab('run')}>运行</button></div>
      {rightTab === 'chat' ? <div className="ai-launch-card"><span className="ai-orb">✦</span><strong>OpenCode 创作助手</strong><p>通过自然语言新增、更新和连接工作流节点。</p><button onClick={() => setChatDialogOpen(true)}>打开 OpenCode 对话框</button></div> : rightTab === 'run' ? <div className="run-panel">
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
          {harnessNodeKinds.includes(selectedNode.data.kind) && selectedHarness?.task && <label>相对工作目录<input value={String(selectedNode.data.relative_path || '.')} onChange={(event) => updateSelected('relative_path', event.target.value)} placeholder="."/></label>}
          {harnessNodeKinds.includes(selectedNode.data.kind) && selectedHarness?.service && <><label>服务路径<input value={String(selectedNode.data.service_path || '')} onChange={(event) => updateSelected('service_path', event.target.value)} placeholder="api/run"/></label><label>HTTP 方法<select value={String(selectedNode.data.method || 'POST')} onChange={(event) => updateSelected('method', event.target.value)}>{['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map((method) => <option key={method}>{method}</option>)}</select></label><label>请求体 JSON<textarea key={`${selectedNode.id}-service-body`} defaultValue={JSON.stringify(selectedNode.data.body || { message: '{{latest}}' }, null, 2)} onBlur={(event) => updateSelectedJson('body', event.target.value)}/></label><label className="check-row"><input type="checkbox" checked={selectedNode.data.auto_start !== false} onChange={(event) => updateSelected('auto_start', event.target.checked)}/>自动准备并启动服务</label></>}
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

    {chatDialogOpen && <section className="opencode-dialog liquid-glass-strong" role="dialog" aria-modal="false" aria-label="OpenCode 工作流创作助手">
      <div className="chat-panel">
        <div className="generator-title"><span className="ai-orb">✦</span><div><strong>OpenCode 创作助手</strong><small>真实模型：{generatorModel}</small></div><button className="dialog-close" onClick={() => setChatDialogOpen(false)} aria-label="关闭对话框">×</button></div>
        <div className="chat-messages">
          {chatMessages.length === 0 && <div className="chat-empty"><p>你可以这样说：</p><button onClick={() => setChatInput('创建一个代码审查流程，先分析代码，再人工审批，最后运行测试')}>创建代码审查流程</button><button onClick={() => setChatInput('在当前流程的验证前增加一个人工审批节点')}>增量修改当前流程</button></div>}
          {chatMessages.map((item, index) => <div key={index} className={`chat-message ${item.role}`}><span>{item.role === 'user' ? '你' : item.role === 'assistant' ? 'AI' : '!'}</span><p>{item.content}</p></div>)}
          {generationId && <div className="thinking"><i/><i/><i/><span>OpenCode 正在生成节点…</span></div>}
        </div>
        <div className="chat-composer"><textarea autoFocus value={chatInput} disabled={!!generationId} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); generateFromChat() } }} placeholder="例如：创建代码审查流程，分析代码后等待人工审批，最后运行测试…"/>
          {generationId ? <button className="stop" onClick={stopGeneration}>停止生成</button> : <button className="send" onClick={generateFromChat} disabled={!chatInput.trim()}>发送并构建工作流</button>}
        </div>
      </div>
    </section>}

    <footer className="console"><span className="status-dot"/><strong>运行控制台</strong><span>{message}</span><span className="console-meta">{nodes.length} 个节点 · {edges.length} 条连线</span></footer>
  </div>
}

export default function App() { return <ReactFlowProvider><StudioCanvas/></ReactFlowProvider> }
