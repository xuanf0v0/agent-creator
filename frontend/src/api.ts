import type { AgentCapability, IntegrationsStatus, NodeTypeInfo, ProjectSpec, ProviderSettings, ProviderProtocol, RuntimeStatus, Workflow, WorkflowRun } from './types'

export class ApiError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

export async function loadSpec(): Promise<{ etag: string; spec: ProjectSpec }> {
  const response = await fetch('/api/spec')
  if (!response.ok) throw new Error('无法读取项目配置')
  return response.json()
}

export async function saveWorkflow(workflow: Workflow, etag: string) {
  const response = await fetch(`/api/workflows/${workflow.id}`, {
    method: 'PUT', headers: { 'content-type': 'application/json', 'if-match': etag }, body: JSON.stringify(workflow),
  })
  const body = await response.json()
  if (!response.ok) throw new ApiError(body.detail || '保存工作流失败', response.status)
  return body as { etag: string; workflow: Workflow }
}

export function loadGeneratorStatus() {
  return jsonRequest<{ backend: 'opencode'; binary: string; binary_error?: string; model: string; ready: boolean; credential_env?: string }>('/api/generator/status')
}

export function loadIntegrationsStatus() {
  return jsonRequest<IntegrationsStatus>('/api/integrations/status')
}

export function loadRuntimeStatus() {
  return jsonRequest<RuntimeStatus>('/api/runtime/status')
}

export function loadProviderSettings() {
  return jsonRequest<ProviderSettings>('/api/settings/provider')
}

export function saveProviderSettings(payload: { protocol: ProviderProtocol; base_url: string; model: string; api_key?: string; clear_api_key?: boolean }) {
  return jsonRequest<ProviderSettings>('/api/settings/provider', {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload),
  })
}

export function clearProviderSettings() {
  return jsonRequest<ProviderSettings>('/api/settings/provider', { method: 'DELETE' })
}

async function jsonRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body))
  return body as T
}

export function startWorkflowRun(workflowId: string, input: string) {
  return jsonRequest<WorkflowRun>(`/api/workflows/${workflowId}/runs`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ input }) })
}

export function loadWorkflowRun(runId: string) {
  return jsonRequest<WorkflowRun>(`/api/workflow-runs/${runId}`)
}

export function cancelWorkflowRun(runId: string) {
  return jsonRequest<WorkflowRun>(`/api/workflow-runs/${runId}/cancel`, { method: 'POST' })
}

export function resolveApproval(runId: string, nodeId: string, approved: boolean, comment = '') {
  return jsonRequest<WorkflowRun>(`/api/workflow-runs/${runId}/nodes/${nodeId}/approval`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ approved, comment }) })
}

// ------------------------------------------------------------------
// Creator Harness API — Dynamic node types and agent capabilities
// ------------------------------------------------------------------

export async function loadNodeTypes(): Promise<{ node_types: NodeTypeInfo[]; total: number }> {
  const response = await fetch('/api/creator/node-types')
  if (!response.ok) throw new Error('无法加载节点类型')
  return response.json()
}

export async function loadCreatorAgents(): Promise<{ agents: AgentCapability[]; total: number }> {
  const response = await fetch('/api/creator/agents')
  if (!response.ok) throw new Error('无法加载智能体能力')
  return response.json()
}

export async function loadCreatorAgent(agentId: string): Promise<AgentCapability> {
  const response = await fetch(`/api/creator/agents/${agentId}`)
  if (!response.ok) throw new Error(`无法加载智能体: ${agentId}`)
  return response.json()
}

export async function loadNodeTypeAgents(nodeType: string): Promise<{ node_type: string; agents: AgentCapability[] }> {
  const response = await fetch(`/api/creator/node-types/${nodeType}/agents`)
  if (!response.ok) throw new Error(`无法加载节点类型 ${nodeType} 的智能体`)
  return response.json()
}

export async function parseIntent(message: string, workflowId?: string, history: Array<{ role: string; content: string }> = []) {
  const response = await fetch('/api/creator/parse-intent', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message, workflow_id: workflowId, history }),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '意图解析失败')
  return body
}

export async function sendCreatorDecide(message: string, workflowId?: string, history: Array<{ role: string; content: string }> = []) {
  const response = await fetch('/api/creator/decide', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message, workflow_id: workflowId, history }),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '创作决策失败')
  return body
}

export async function sendCreatorGenerate(message: string, workflowId?: string, name?: string) {
  const response = await fetch('/api/creator/generate', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ message, workflow_id: workflowId, name }),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '无法启动生成器')
  return body as { generation_id: string; workflow_id: string }
}

export async function loadCreatorGenerations(workflowId: string): Promise<Array<{ id: string; workflow_id: string; status: string }>> {
  const response = await fetch(`/api/creator/workflows/${workflowId}/generations`)
  if (!response.ok) return []
  const body = await response.json()
  return body.generations || []
}

export async function loadCreatorChatStatus(workflowId: string): Promise<{ has_history: boolean; active_generation: { id: string; status: string } | null }> {
  const response = await fetch(`/api/creator/workflows/${workflowId}/chat-status`)
  if (!response.ok) return { has_history: false, active_generation: null }
  return response.json()
}

export async function loadCreatorMessages(workflowId: string): Promise<Array<{ role: 'user' | 'assistant'; content: string; options?: string[] }>> {
  const response = await fetch(`/api/creator/workflows/${workflowId}/messages`)
  if (!response.ok) return []
  return response.json()
}

export async function cancelCreatorGeneration(generationId: string) {
  const response = await fetch(`/api/creator/generations/${generationId}/cancel`, { method: 'POST' })
  if (!response.ok) throw new Error('取消生成失败')
  return response.json()
}
