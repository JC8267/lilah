import { create } from 'zustand';
import type { Conversation, Message, VegaLiteSpec } from '../types';

interface ChatState {
  conversations: Conversation[];
  activeConversationId: string | null;
  messages: Message[];
  streamingText: string;
  streamingCharts: VegaLiteSpec[];
  isStreaming: boolean;
  toolStatus: { tool: string; status: 'running' | 'done' } | null;

  setConversations: (convs: Conversation[]) => void;
  setActiveConversation: (id: string | null) => void;
  addConversation: (conv: Conversation) => void;
  updateConversationTitle: (id: string, title: string) => void;
  removeConversation: (id: string) => void;
  setMessages: (msgs: Message[]) => void;
  addMessage: (msg: Message) => void;
  appendStreamingText: (text: string) => void;
  addStreamingChart: (spec: VegaLiteSpec) => void;
  setToolStatus: (status: ChatState['toolStatus']) => void;
  startStreaming: () => void;
  finishStreaming: (text: string, charts: VegaLiteSpec[]) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  activeConversationId: null,
  messages: [],
  streamingText: '',
  streamingCharts: [],
  isStreaming: false,
  toolStatus: null,

  setConversations: (convs) => set({ conversations: convs }),

  setActiveConversation: (id) => set({ activeConversationId: id }),

  addConversation: (conv) =>
    set((s) => ({ conversations: [conv, ...s.conversations] })),

  updateConversationTitle: (id, title) =>
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, title } : c
      ),
    })),

  removeConversation: (id) =>
    set((s) => ({
      conversations: s.conversations.filter((c) => c.id !== id),
      activeConversationId: s.activeConversationId === id ? null : s.activeConversationId,
    })),

  setMessages: (msgs) => set({ messages: msgs }),

  addMessage: (msg) => set((s) => ({ messages: [...s.messages, msg] })),

  appendStreamingText: (text) =>
    set((s) => ({ streamingText: s.streamingText + text })),

  addStreamingChart: (spec) =>
    set((s) => ({ streamingCharts: [...s.streamingCharts, spec] })),

  setToolStatus: (status) => set({ toolStatus: status }),

  startStreaming: () =>
    set({ isStreaming: true, streamingText: '', streamingCharts: [], toolStatus: null }),

  finishStreaming: (text, charts) => {
    const state = get();
    const convId = state.activeConversationId || '';
    if (text || charts.length > 0) {
      const msg: Message = {
        id: crypto.randomUUID(),
        conversation_id: convId,
        role: 'assistant',
        content: text || 'Chart generated.',
        charts: charts.length > 0 ? charts : undefined,
        created_at: new Date().toISOString(),
      };
      set((s) => ({
        messages: [...s.messages, msg],
        isStreaming: false,
        streamingText: '',
        streamingCharts: [],
        toolStatus: null,
      }));
    } else {
      set({ isStreaming: false, streamingText: '', streamingCharts: [], toolStatus: null });
    }
  },
}));
