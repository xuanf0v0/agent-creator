export type NodeKind = 'agent' | 'prompt' | 'condition' | 'parallel' | 'loop' | 'approval' | 'validator' | 'output'
export type WorkflowNode = { id: string; type: NodeKind; data: Record<string, unknown>; position: { x: number; y: number } }
export type WorkflowEdge = { source: string; target: string; condition?: string | null }
export type Workflow = { id: string; name: string; nodes: WorkflowNode[]; edges: WorkflowEdge[] }
export type Agent = { id: string; name: string; description?: string; model?: string }
export type ProjectSpec = { version: '1'; name: string; description?: string; project_dir: string; agents: Agent[]; providers: unknown[]; harness: unknown[]; workflows: Workflow[] }
