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
