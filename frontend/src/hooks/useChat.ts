import { useCallback, useEffect } from 'react';
import { useChatStore } from '../stores/chat-store';
import { useFilterStore } from '../stores/filter-store';
import type {
  DemoFilter,
  Message,
  MessageMetadata,
  ToolTraceEvent,
  VegaLiteSpec,
} from '../types';

let activeAbortController: AbortController | null = null;
let activeStreamId: string | null = null;

function buildFilterSnapshot(filters: DemoFilter[]): DemoFilter[] {
  return filters.slice(0, 1).map((filter) => ({
    demo_id: filter.demo_id,
    demo_level: filter.demo_level,
  }));
}

function summarizeToolResult(result: unknown): string | undefined {
  if (!result || typeof result !== 'object') return undefined;

  const payload = result as Record<string, unknown>;
  if (typeof payload.error === 'string' && payload.error.trim()) {
    return payload.error.trim();
  }
  if (typeof payload.analysis_type === 'string' && payload.analysis_type.trim()) {
    return payload.analysis_type.replace(/_/g, ' ');
  }
  if (typeof payload.question_id === 'string' && payload.question_id.trim()) {
    return payload.question_id.trim();
  }
  if (typeof payload.question_group === 'string' && payload.question_group.trim()) {
    return payload.question_group.trim();
  }

  return undefined;
}

async function buildErrorMessage(res: Response): Promise<string> {
  try {
    const payload = (await res.json()) as { detail?: string; request_id?: string };
    if (payload.request_id) {
      return payload.detail
        ? `${payload.detail} (request ${payload.request_id})`
        : `Request failed (request ${payload.request_id})`;
    }
    if (payload.detail) {
      return payload.detail;
    }
  } catch {
    // Fall back to status text below.
  }

  return res.statusText || `HTTP ${res.status}`;
}

export function abortActiveChatStream() {
  const streamId = activeStreamId;
  activeAbortController?.abort();
  activeAbortController = null;
  activeStreamId = null;

  if (streamId) {
    useChatStore.getState().cancelStreaming(streamId);
  }
}

export function useChat() {
  const {
    startStreaming,
    appendStreamingText,
    addStreamingChart,
    addStreamingToolEvent,
    completeStreamingToolEvent,
    setStreamingConversation,
    finishStreaming,
    cancelStreaming,
    addMessage,
    setActiveConversation,
    addConversation,
    updateConversationTitle,
  } = useChatStore();

  const activeFilters = useFilterStore((state) => state.activeFilters);

  useEffect(() => {
    return () => {
      abortActiveChatStream();
    };
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      const current = useChatStore.getState();
      if (current.isStreaming) {
        abortActiveChatStream();
      }

      const conversationId = current.activeConversationId;
      const streamId = crypto.randomUUID();
      const filterSnapshot = buildFilterSnapshot(activeFilters);

      const userMsg: Message = {
        id: crypto.randomUUID(),
        conversation_id: conversationId || '',
        role: 'user',
        content: trimmed,
        metadata: filterSnapshot.length > 0 ? { active_filters: filterSnapshot } : undefined,
        created_at: new Date().toISOString(),
      };
      addMessage(userMsg);
      startStreaming({ streamId, conversationId, filters: filterSnapshot });

      const filtersPayload: Record<string, string> = {};
      for (const filter of filterSnapshot) {
        filtersPayload[filter.demo_id] = filter.demo_level;
      }

      const controller = new AbortController();
      activeAbortController = controller;
      activeStreamId = streamId;

      const finishIfActive = (
        finalText: string,
        finalCharts: VegaLiteSpec[],
        metadata?: MessageMetadata
      ) => {
        if (useChatStore.getState().streamId !== streamId) return;
        finishStreaming({
          streamId,
          text: finalText,
          charts: finalCharts,
          metadata,
        });
        if (activeStreamId === streamId) {
          activeAbortController = null;
          activeStreamId = null;
        }
      };

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({
            conversation_id: conversationId,
            message: trimmed,
            filters: filterSnapshot.length > 0 ? filtersPayload : undefined,
          }),
        });

        if (!res.ok) {
          const message = await buildErrorMessage(res);
          finishIfActive(`Error: ${message}`, []);
          return;
        }

        const reader = res.body?.getReader();
        if (!reader) {
          finishIfActive('Error: No response body', []);
          return;
        }

        const decoder = new TextDecoder();
        let buffer = '';
        let collectedText = '';
        const collectedCharts: VegaLiteSpec[] = [];
        let lastEventAt = Date.now();
        const streamTimeoutMs = 90000;
        let terminalEventSeen = false;

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          buffer = buffer.replace(/\r/g, '');
          lastEventAt = Date.now();

          while (true) {
            const sepIndex = buffer.indexOf('\n\n');
            if (sepIndex === -1) break;

            const rawEvent = buffer.slice(0, sepIndex);
            buffer = buffer.slice(sepIndex + 2);
            if (!rawEvent.trim()) continue;

            const lines = rawEvent.split('\n');
            let eventType = 'message';
            const dataLines: string[] = [];

            for (const rawLine of lines) {
              const line = rawLine.replace(/\r$/, '');
              if (line.startsWith('event:')) {
                eventType = line.slice(6).trim() || 'message';
              } else if (line.startsWith('data:')) {
                dataLines.push(line.slice(5).trimStart());
              }
            }

            if (dataLines.length === 0) continue;

            const dataStr = dataLines.join('\n');
            try {
              const data = JSON.parse(dataStr) as Record<string, unknown>;

              switch (eventType) {
                case 'text_delta':
                  collectedText += String(data.content || '');
                  appendStreamingText(streamId, String(data.content || ''));
                  break;

                case 'tool_start':
                  addStreamingToolEvent(streamId, {
                    kind: 'tool',
                    label: String(data.tool || 'tool'),
                    status: 'running',
                    input:
                      data.input && typeof data.input === 'object'
                        ? (data.input as Record<string, unknown>)
                        : undefined,
                    created_at: Date.now(),
                    updated_at: Date.now(),
                  });
                  break;

                case 'tool_result':
                  completeStreamingToolEvent(
                    streamId,
                    String(data.tool || 'tool'),
                    data.result &&
                      typeof data.result === 'object' &&
                      'error' in (data.result as Record<string, unknown>)
                      ? 'error'
                      : 'done',
                    summarizeToolResult(data.result)
                  );
                  break;

                case 'status':
                  if (data.message) {
                    const event: ToolTraceEvent = {
                      kind: 'status',
                      label: String(data.message),
                      status: 'done',
                      created_at: Date.now(),
                      updated_at: Date.now(),
                    };
                    addStreamingToolEvent(streamId, event);
                  }
                  break;

                case 'chart':
                  if (data.spec) {
                    const spec = data.spec as VegaLiteSpec;
                    collectedCharts.push(spec);
                    addStreamingChart(streamId, spec);
                  }
                  break;

                case 'conversation':
                  if (data.id && data.title) {
                    const resolvedId = String(data.id);
                    const resolvedTitle = String(data.title);
                    setStreamingConversation(streamId, resolvedId);

                    const state = useChatStore.getState();
                    const existing = state.conversations.find((conv) => conv.id === resolvedId);
                    if (!existing) {
                      addConversation({
                        id: resolvedId,
                        title: resolvedTitle,
                        created_at: new Date().toISOString(),
                        updated_at: new Date().toISOString(),
                      });
                    } else {
                      updateConversationTitle(resolvedId, resolvedTitle);
                    }

                    if (!state.activeConversationId || state.activeConversationId === conversationId) {
                      setActiveConversation(resolvedId);
                    }
                  }
                  break;

                case 'done':
                  terminalEventSeen = true;
                  finishIfActive(
                    collectedText || (typeof data.text === 'string' ? data.text : ''),
                    collectedCharts.length > 0
                      ? collectedCharts
                      : Array.isArray(data.charts)
                        ? (data.charts as VegaLiteSpec[])
                        : [],
                    typeof data.request_id === 'string'
                      ? { request_id: data.request_id }
                      : undefined
                  );
                  return;

                case 'error':
                  terminalEventSeen = true;
                  finishIfActive(
                    `Error: ${data.message || 'Unknown error'}`,
                    [],
                    typeof data.request_id === 'string'
                      ? { request_id: data.request_id }
                      : undefined
                  );
                  return;
              }
            } catch {
              // Skip malformed event payloads.
            }

            lastEventAt = Date.now();
          }

          if (Date.now() - lastEventAt > streamTimeoutMs) {
            finishIfActive('Error: Stream timed out waiting for response.', collectedCharts);
            return;
          }
        }

        if (!terminalEventSeen) {
          if (collectedText || collectedCharts.length > 0) {
            finishIfActive(collectedText, collectedCharts);
          } else {
            finishIfActive('Error: Stream ended before a final response was received.', []);
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          if (activeStreamId === streamId) {
            activeAbortController = null;
            activeStreamId = null;
          }
          cancelStreaming(streamId);
          return;
        }

        finishIfActive(
          `Error: ${err instanceof Error ? err.message : 'Network error'}`,
          []
        );
      }
    },
    [
      activeFilters,
      addConversation,
      addMessage,
      addStreamingChart,
      addStreamingToolEvent,
      appendStreamingText,
      cancelStreaming,
      completeStreamingToolEvent,
      finishStreaming,
      setActiveConversation,
      setStreamingConversation,
      startStreaming,
      updateConversationTitle,
    ]
  );

  return { sendMessage, stopStreaming: abortActiveChatStream };
}
