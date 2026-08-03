import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Background, Controls, MiniMap, Panel, ReactFlow, ReactFlowProvider,
  addEdge, applyEdgeChanges, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type EdgeChange, type Node, type NodeChange,
} from '@xyflow/react'
import { cancelGeneration, loadGeneratorMessages, loadSpec, saveWorkflow, sendGeneratorMessage } from './api'
import type { NodeKind, ProjectSpec, Workflow } from './types'

const nodeCatalog: Array<{ type: NodeKind; label: string; icon: string; description: string }> = [
  { type: 'agent', label: '智能体', icon: '✦', description: '调用一个智能体完成任务' },
  { type: 'prompt', label: '提示词', icon: 'T', description: '组织输入内容和上下文' },
  { type: 'condition', label: '条件判断', icon: '◇', description: '根据结果选择执行分支' },
  { type: 'parallel', label: '并行执行', icon: '⑂', description: '同时运行多个分支' },
  { type: 'loop', label: '循环', icon: '↻', description: '按次数重复执行步骤' },
  { type: 'approval', label: '人工审批', icon: '✓', description: '暂停并等待用户确认' },
  { type: 'validator', label: '验证器', icon: '⌁', description: '运行测试或完成检查' },
  { type: 'output', label: '输出结果', icon: '→', description: '展示工作流最终结果' },
]

type CanvasNode = Node<{ label: string; description: string; kind: NodeKind; agent_id?: string }>
type ChatMessage = { role: 'user' | 'assistant' | 'system'; content: string }

const nodeStyle = { background: '#172033', color: '#eef3ff', border: '1px solid #415273', borderRadius: 12, width: 190 }

function nodeLabel(kind: NodeKind) {
  return nodeCatalog.find((item) => item.type === kind)?.label ?? kind
}

function toCanvas(workflow: Workflow): { nodes: CanvasNode[]; edges: Edge[] } {
  return {
    nodes: workflow.nodes.map((item) => ({
      id: item.id, type: 'default', position: item.position,
      data: {
        label: String(item.data.description || nodeLabel(item.type)),
        description: String(item.data.description || ''), kind: item.type,
        agent_id: typeof item.data.agent_id === 'string' ? item.data.agent_id : undefined,
      },
      style: nodeStyle,
    })),
    edges: workflow.edges.map((item, index) => ({ id: `e-${index}-${item.source}-${item.target}`, source: item.source, target: item.target, animated: true, style: { stroke: '#6d8cff' } })),
  }
}

function StudioCanvas() {
  const [spec, setSpec] = useState<ProjectSpec | null>(null)
  const [etag, setEtag] = useState('')
  const [workflowId, setWorkflowId] = useState('')
  const [nodes, setNodes] = useState<CanvasNode[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [message, setMessage] = useState('正在载入项目…')
  const [saving, setSaving] = useState(false)
  const [rightTab, setRightTab] = useState<'chat' | 'node'>('chat')
  const [chatInput, setChatInput] = useState('')
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([])
  const [generationId, setGenerationId] = useState<string | null>(null)
  const { screenToFlowPosition, fitView } = useReactFlow()

  const workflow = useMemo(() => spec?.workflows.find((item) => item.id === workflowId), [spec, workflowId])
  const selectedNode = nodes.find((item) => item.id === selected)

  const openWorkflow = useCallback((project: ProjectSpec, id: string) => {
    const item = project.workflows.find((flow) => flow.id === id)
    if (!item) return
    const view = toCanvas(item)
    setNodes(view.nodes); setEdges(view.edges); setWorkflowId(id); setSelected(null)
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

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => setNodes((items) => applyNodeChanges(changes, items)), [])
  const onEdgesChange = useCallback((changes: EdgeChange[]) => setEdges((items) => applyEdgeChanges(changes, items)), [])
  const onConnect = useCallback((connection: Connection) => setEdges((items) => addEdge({ ...connection, animated: true, style: { stroke: '#6d8cff' } }, items)), [])

  function onDrop(event: React.DragEvent) {
    event.preventDefault()
    const kind = event.dataTransfer.getData('application/openagent-node') as NodeKind
    if (!kind) return
    const catalog = nodeCatalog.find((item) => item.type === kind)!
    const position = screenToFlowPosition({ x: event.clientX, y: event.clientY })
    const id = `${kind}-${crypto.randomUUID().slice(0, 8)}`
    setNodes((items) => [...items, {
      id, position, type: 'default', data: { kind, label: catalog.label, description: catalog.description },
      style: nodeStyle,
    }])
  }

  function updateSelected(field: string, value: string) {
    if (!selected) return
    setNodes((items) => items.map((node) => node.id === selected ? { ...node, data: { ...node.data, [field]: value, ...(field === 'description' ? { label: value || nodeLabel(node.data.kind) } : {}) } } : node))
  }

  async function persist() {
    if (!workflow || !spec) return
    setSaving(true); setMessage('正在保存工作流…')
    const payload: Workflow = {
      ...workflow,
      nodes: nodes.map((node) => ({ id: node.id, type: node.data.kind, position: node.position, data: { description: node.data.description, ...(node.data.agent_id ? { agent_id: node.data.agent_id } : {}) } })),
      edges: edges.map((edge) => ({ source: edge.source, target: edge.target })),
    }
    try {
      const result = await saveWorkflow(payload, etag)
      setEtag(result.etag)
      setSpec({ ...spec, workflows: spec.workflows.map((item) => item.id === workflow.id ? result.workflow : item) })
      setMessage('工作流已保存')
    } catch (error) { setMessage(error instanceof Error ? error.message : '保存失败') }
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
          data: { kind: node.type, label: node.data.description || nodeLabel(node.type), description: node.data.description || '', agent_id: node.data.agent_id },
          style: { ...nodeStyle, border: '1px solid #79d7ad', boxShadow: '0 0 22px #36c98a55' },
        }])
        setMessage(`已生成节点：${node.data.description || node.id}`)
      })
      listen('workflow.node.updated', ({ node }) => setNodes((items) => items.map((item) => item.id === node.id ? { ...item, data: { ...item.data, ...node.data, label: node.data.description || item.data.label } } : item)))
      listen('workflow.node.deleted', ({ node_id }) => { setNodes((items) => items.filter((item) => item.id !== node_id)); setEdges((items) => items.filter((edge) => edge.source !== node_id && edge.target !== node_id)) })
      listen('workflow.edge.added', ({ edge }) => setEdges((items) => items.some((item) => item.source === edge.source && item.target === edge.target) ? items : addEdge({ ...edge, id: `ai-${crypto.randomUUID()}`, animated: true, style: { stroke: '#79d7ad' } }, items)))
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

  return <div className="studio-shell">
    <header className="topbar">
      <div className="brand"><span className="logo">OA</span><div><strong>OpenAgent Studio</strong><small>{spec?.name ?? '智能体工作流'}</small></div></div>
      <div className="top-actions">
        <select value={workflowId} onChange={(event) => spec && openWorkflow(spec, event.target.value)}>
          {spec?.workflows.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button className="ghost" onClick={() => fitView({ padding: 0.22 })}>适应画布</button>
        <button className="primary" disabled={saving || !workflow} onClick={persist}>{saving ? '保存中…' : '保存工作流'}</button>
        <button className="run" onClick={() => setMessage('执行器尚未接入；当前版本支持可视化编排、连线和保存')}>▶ 运行</button>
      </div>
    </header>

    <aside className="node-library">
      <div className="panel-title"><strong>节点库</strong><span>拖入画布开始编排</span></div>
      <div className="library-list">{nodeCatalog.map((item) => <div key={item.type} className="library-item" draggable onDragStart={(event) => { event.dataTransfer.setData('application/openagent-node', item.type); event.dataTransfer.effectAllowed = 'move' }}>
        <span className={`node-icon ${item.type}`}>{item.icon}</span><div><strong>{item.label}</strong><small>{item.description}</small></div>
      </div>)}</div>
    </aside>

    <main className="canvas-area" onDrop={onDrop} onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = 'move' }}>
      <ReactFlow nodes={nodes} edges={edges} onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect} onNodeClick={(_, node) => { setSelected(node.id); setRightTab('node') }} onPaneClick={() => setSelected(null)} fitView>
        <Background color="#32405a" gap={22} size={1}/><Controls/><MiniMap pannable zoomable nodeColor="#6686ff"/>
        <Panel position="top-left" className="canvas-hint">拖拽节点并连接端点，构建智能体执行流程</Panel>
      </ReactFlow>
    </main>

    <aside className="inspector">
      <div className="inspector-tabs"><button className={rightTab === 'chat' ? 'active' : ''} onClick={() => setRightTab('chat')}>AI 创建</button><button className={rightTab === 'node' ? 'active' : ''} onClick={() => setRightTab('node')}>节点设置</button></div>
      {rightTab === 'chat' ? <div className="chat-panel">
        <div className="generator-title"><span className="ai-orb">✦</span><div><strong>OpenCode 创作助手</strong><small>描述需求，我会实时搭建画布</small></div></div>
        <div className="chat-messages">
          {chatMessages.length === 0 && <div className="chat-empty"><p>你可以这样说：</p><button onClick={() => setChatInput('创建一个代码审查流程，先分析代码，再人工审批，最后运行测试')}>创建代码审查流程</button><button onClick={() => setChatInput('在当前流程的验证前增加一个人工审批节点')}>增量修改当前流程</button></div>}
          {chatMessages.map((item, index) => <div key={index} className={`chat-message ${item.role}`}><span>{item.role === 'user' ? '你' : item.role === 'assistant' ? 'AI' : '!'}</span><p>{item.content}</p></div>)}
          {generationId && <div className="thinking"><i/><i/><i/><span>OpenCode 正在生成节点…</span></div>}
        </div>
        <div className="chat-composer"><textarea value={chatInput} disabled={!!generationId} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); generateFromChat() } }} placeholder="描述你想创建的智能体工作流…"/>
          {generationId ? <button className="stop" onClick={stopGeneration}>停止生成</button> : <button className="send" onClick={generateFromChat} disabled={!chatInput.trim()}>发送需求</button>}
        </div>
      </div> : <>
        <div className="panel-title"><strong>节点设置</strong><span>{selectedNode ? nodeLabel(selectedNode.data.kind) : '请选择一个节点'}</span></div>
        {selectedNode ? <div className="form-stack">
          <label>节点名称<input value={selectedNode.data.description} onChange={(event) => updateSelected('description', event.target.value)}/></label>
          <label>节点类型<input value={nodeLabel(selectedNode.data.kind)} disabled/></label>
          {selectedNode.data.kind === 'agent' && <label>选择智能体<select value={selectedNode.data.agent_id || ''} onChange={(event) => updateSelected('agent_id', event.target.value)}><option value="">请选择</option>{spec?.agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>}
          <label>节点编号<input value={selectedNode.id} disabled/></label>
          <button className="danger" onClick={() => { setNodes((items) => items.filter((node) => node.id !== selectedNode.id)); setEdges((items) => items.filter((edge) => edge.source !== selectedNode.id && edge.target !== selectedNode.id)); setSelected(null) }}>删除节点</button>
        </div> : <div className="empty-state"><span>◇</span><p>点击画布中的节点<br/>在这里编辑详细参数</p></div>}
      </>}
    </aside>

    <footer className="console"><span className="status-dot"/><strong>运行控制台</strong><span>{message}</span><span className="console-meta">{nodes.length} 个节点 · {edges.length} 条连线</span></footer>
  </div>
}

export default function App() { return <ReactFlowProvider><StudioCanvas/></ReactFlowProvider> }
