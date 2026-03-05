export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant';
  content: string;
  charts?: VegaLiteSpec[];
  metadata?: MessageMetadata;
  created_at: string;
}

export type VegaLiteSpec = Record<string, unknown>;

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
}

export interface DemoFilter {
  demo_id: string;
  demo_level: string;
}

export interface DemoDimension {
  demo_id: string;
  demo_level: string;
}

export interface ToolTraceEvent {
  kind: 'status' | 'tool';
  label: string;
  status: 'running' | 'done' | 'error';
  detail?: string;
  input?: Record<string, unknown>;
  created_at: number;
  updated_at: number;
}

export interface MessageMetadata {
  active_filters?: DemoFilter[];
  tool_events?: ToolTraceEvent[];
  request_id?: string;
}
