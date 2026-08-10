import type { IntegrationsStatus, ProjectSpec, RuntimeStatus, Workflow, WorkflowRun } from './types'

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

export async function sendGeneratorMessage(workflowId: string, message: string) {
  const response = await fetch(`/api/generator/workflows/${workflowId}/messages`, {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ message }),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '无法启动智能体生成器')
  return body as { generation_id: string; workflow_id: string }
}

export async function optimizeWorkflow(workflowId: string) {
  const response = await fetch(`/api/generator/workflows/${workflowId}/optimize`, { method: 'POST' })
  const body = await response.json()
  if (!response.ok) throw new Error(body.detail || '无法启动工作流优化')
  return body as { generation_id: string; workflow_id: string }
}

export async function loadGeneratorMessages(workflowId: string) {
  const response = await fetch(`/api/generator/workflows/${workflowId}/messages`)
  if (!response.ok) return []
  return response.json() as Promise<Array<{ role: 'user' | 'assistant'; content: string; options?: string[] }>>
}

export function loadGeneratorStatus() {
  return jsonRequest<{ backend: 'opencode'; binary: string; model: string; ready: boolean; credential_env?: string }>('/api/generator/status')
}

export function loadIntegrationsStatus() {
  return jsonRequest<IntegrationsStatus>('/api/integrations/status')
}

export function loadRuntimeStatus() {
  return jsonRequest<RuntimeStatus>('/api/runtime/status')
}

export async function cancelGeneration(generationId: string) {
  await fetch(`/api/generator/generations/${generationId}/cancel`, { method: 'POST' })
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

export function cancelWorkflowRun(runId: string) {
  return jsonRequest<WorkflowRun>(`/api/workflow-runs/${runId}/cancel`, { method: 'POST' })
}

export function resolveApproval(runId: string, nodeId: string, approved: boolean, comment = '') {
  return jsonRequest<WorkflowRun>(`/api/workflow-runs/${runId}/nodes/${nodeId}/approval`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ approved, comment }) })
}
