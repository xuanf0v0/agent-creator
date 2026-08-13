export type NodeKind = 'manual_trigger' | 'webhook' | 'schedule' | 'llm' | 'agent' | 'knowledge_retrieval' | 'tool' | 'http_request' | 'code' | 'prompt' | 'variable_set' | 'transform' | 'merge' | 'condition' | 'switch' | 'parallel' | 'iteration' | 'loop' | 'approval' | 'validator' | 'subworkflow' | 'delay' | 'output'
export type WorkflowNode = { id: string; type: NodeKind; data: Record<string, unknown>; position: { x: number; y: number } }
export type WorkflowEdge = { source: string; target: string; condition?: string | null }
export type EvaluationAssertion = { path: string; operator: 'exists' | 'equals' | 'contains' | 'matches' | 'type'; expected?: unknown }
export type EvaluationMock = { node_id: string; response?: unknown }
export type EvaluationCase = { id: string; name: string; enabled: boolean; input: unknown; assertions: EvaluationAssertion[]; semantic_criteria: string[]; approvals: Record<string, boolean>; mocks: EvaluationMock[]; timeout_seconds: number }
export type Workflow = { id: string; name: string; nodes: WorkflowNode[]; edges: WorkflowEdge[]; evaluation?: { cases: EvaluationCase[] } }
export type Agent = { id: string; name: string; description?: string; model?: string }
export type Harness = { id: string; name: string; description?: string; backend_id: string; agent_id: string; labels?: Record<string, string> }
export type ProjectSpec = { version: '1'; name: string; description?: string; project_dir: string; agents: Agent[]; providers: unknown[]; harness: Harness[]; workflows: Workflow[] }
export type NodeRunState = { status: 'pending' | 'running' | 'waiting' | 'completed' | 'failed' | 'skipped'; output?: unknown; input?: unknown; error?: string; started_at?: number; completed_at?: number; warning?: string }
export type WorkflowRun = { id: string; workflow_id: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'; input: unknown; node_states: Record<string, NodeRunState>; outputs: Record<string, unknown>; waiting_approvals: string[]; error: string; error_code?: string }
export type IntegrationStatus = { id: string; name: string; workflow_id: string; ready: boolean; missing_env: string[]; auto_reply: boolean }
export type IntegrationsStatus = { feishu: IntegrationStatus[]; qq: IntegrationStatus[] }
export type RuntimeStatus = { running: boolean; backends: Record<string, { actionable_error?: string; identity_mismatch?: string[]; readiness?: Record<string, { state?: string; error_code?: string; accepts_tasks?: boolean }>; task_agent_error?: string }> }

// Creator Harness types
export type NodeTypeInfo = {
  type: NodeKind
  label: string
  category: string
  icon: string
  description: string
  requires_agent?: boolean
  default_data?: Record<string, unknown>
  color?: string
}

export type AgentCapability = {
  agent_id: string
  name: string
  description: string
  capability: string
  sandbox: string
  supported_node_types: NodeKind[]
  backend_id: string
  ready: boolean
}
