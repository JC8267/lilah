import { create } from 'zustand';
import type {
  Conversation,
  DemoFilter,
  Message,
  MessageMetadata,
  ToolTraceEvent,
  VegaLiteSpec,
} from '../types';

interface ChatState {
  conversations: Conversation[];
  activeConversationId: string | null;
  messages: Message[];
  streamId: string | null;
  streamConversationId: string | null;
  streamingText: string;
  streamingCharts: VegaLiteSpec[];
  streamingFilters: DemoFilter[];
  streamingToolEvents: ToolTraceEvent[];
  isStreaming: boolean;
  conversationsLoading: boolean;

  setConversations: (convs: Conversation[]) => void;
  setConversationsLoading: (loading: boolean) => void;
  setActiveConversation: (id: string | null) => void;
  addConversation: (conv: Conversation) => void;
  updateConversationTitle: (id: string, title: string) => void;
  removeConversation: (id: string) => void;
  setMessages: (msgs: Message[]) => void;
  addMessage: (msg: Message) => void;
  appendStreamingText: (streamId: string, text: string) => void;
  addStreamingChart: (streamId: string, spec: VegaLiteSpec) => void;
  addStreamingToolEvent: (streamId: string, event: ToolTraceEvent) => void;
  completeStreamingToolEvent: (
    streamId: string,
    label: string,
    status: 'done' | 'error',
    detail?: string
  ) => void;
  setStreamingConversation: (streamId: string, conversationId: string) => void;
  startStreaming: (payload: {
    streamId: string;
    conversationId: string | null;
    filters: DemoFilter[];
  }) => void;
  cancelStreaming: (streamId?: string) => void;
  finishStreaming: (payload: {
    streamId: string;
    text: string;
    charts: VegaLiteSpec[];
    metadata?: MessageMetadata;
  }) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  conversations: [],
  activeConversationId: null,
  messages: [],
  streamId: null,
  streamConversationId: null,
  streamingText: '',
  streamingCharts: [],
  streamingFilters: [],
  streamingToolEvents: [],
  isStreaming: false,
  conversationsLoading: false,

  setConversations: (convs) => set({ conversations: convs }),
  setConversationsLoading: (loading) => set({ conversationsLoading: loading }),

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

  appendStreamingText: (streamId, text) =>
    set((s) =>
      s.streamId === streamId
        ? { streamingText: s.streamingText + text }
        : s
    ),

  addStreamingChart: (streamId, spec) =>
    set((s) =>
      s.streamId === streamId
        ? { streamingCharts: [...s.streamingCharts, spec] }
        : s
    ),

  addStreamingToolEvent: (streamId, event) =>
    set((s) =>
      s.streamId === streamId
        ? { streamingToolEvents: [...s.streamingToolEvents, event] }
        : s
    ),

  completeStreamingToolEvent: (streamId, label, status, detail) =>
    set((s) => {
      if (s.streamId !== streamId) return s;
      let updated = false;
      const nextEvents = [...s.streamingToolEvents];
      for (let i = nextEvents.length - 1; i >= 0; i -= 1) {
        const event = nextEvents[i];
        if (event.kind === 'tool' && event.label === label && event.status === 'running') {
          nextEvents[i] = {
            ...event,
            status,
            detail: detail || event.detail,
            updated_at: Date.now(),
          };
          updated = true;
          break;
        }
      }

      if (!updated) {
        nextEvents.push({
          kind: 'tool',
          label,
          status,
          detail,
          created_at: Date.now(),
          updated_at: Date.now(),
        });
      }

      return { streamingToolEvents: nextEvents };
    }),

  setStreamingConversation: (streamId, conversationId) =>
    set((s) =>
      s.streamId === streamId
        ? { streamConversationId: conversationId }
        : s
    ),

  startStreaming: ({ streamId, conversationId, filters }) =>
    set({
      streamId,
      streamConversationId: conversationId,
      isStreaming: true,
      streamingText: '',
      streamingCharts: [],
      streamingFilters: filters,
      streamingToolEvents: [],
    }),

  cancelStreaming: (streamId) =>
    set((s) => {
      if (streamId && s.streamId !== streamId) {
        return s;
      }

      return {
        streamId: null,
        streamConversationId: null,
        isStreaming: false,
        streamingText: '',
        streamingCharts: [],
        streamingFilters: [],
        streamingToolEvents: [],
      };
    }),

  finishStreaming: ({ streamId, text, charts, metadata }) => {
    const state = get();
    if (state.streamId !== streamId) {
      return;
    }

    const convId = state.streamConversationId || state.activeConversationId || '';
    const mergedMetadata: MessageMetadata | undefined =
      state.streamingFilters.length > 0 || state.streamingToolEvents.length > 0 || metadata
        ? {
            ...(metadata || {}),
            active_filters: metadata?.active_filters || state.streamingFilters,
            tool_events: metadata?.tool_events || state.streamingToolEvents,
          }
        : undefined;

    if (text || charts.length > 0) {
      const msg: Message = {
        id: crypto.randomUUID(),
        conversation_id: convId,
        role: 'assistant',
        content: text || 'Chart generated.',
        charts: charts.length > 0 ? charts : undefined,
        metadata: mergedMetadata,
        created_at: new Date().toISOString(),
      };
      set((s) => ({
        messages: [...s.messages, msg],
        streamId: null,
        streamConversationId: null,
        isStreaming: false,
        streamingText: '',
        streamingCharts: [],
        streamingFilters: [],
        streamingToolEvents: [],
      }));
    } else {
      set({
        streamId: null,
        streamConversationId: null,
        isStreaming: false,
        streamingText: '',
        streamingCharts: [],
        streamingFilters: [],
        streamingToolEvents: [],
      });
    }
  },
}));
