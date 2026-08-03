import type { ProjectSpec, Workflow } from './types'

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
  if (!response.ok) throw new Error(body.detail || '保存工作流失败')
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

export async function loadGeneratorMessages(workflowId: string) {
  const response = await fetch(`/api/generator/workflows/${workflowId}/messages`)
  if (!response.ok) return []
  return response.json() as Promise<Array<{ role: 'user' | 'assistant'; content: string }>>
}

export async function cancelGeneration(generationId: string) {
  await fetch(`/api/generator/generations/${generationId}/cancel`, { method: 'POST' })
}
