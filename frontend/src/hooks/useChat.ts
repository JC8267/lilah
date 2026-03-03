import { useCallback, useEffect, useRef } from 'react';
import { useChatStore } from '../stores/chat-store';
import { useFilterStore } from '../stores/filter-store';
import type { Message, VegaLiteSpec } from '../types';

export function useChat() {
  const {
    startStreaming,
    appendStreamingText,
    addStreamingChart,
    setToolStatus,
    finishStreaming,
    addMessage,
    setActiveConversation,
    addConversation,
    updateConversationTitle,
  } = useChatStore();

  const activeFilters = useFilterStore((s) => s.activeFilters);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim()) return;

      const current = useChatStore.getState();
      if (current.isStreaming) {
        abortRef.current?.abort();
      }
      const conversationId = current.activeConversationId;

      const userMsg: Message = {
        id: crypto.randomUUID(),
        conversation_id: conversationId || '',
        role: 'user',
        content: text,
        created_at: new Date().toISOString(),
      };
      addMessage(userMsg);
      startStreaming();

      const filters: Record<string, string> = {};
      for (const f of activeFilters) {
        filters[f.demo_id] = f.demo_level;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      const finishIfActive = (finalText: string, finalCharts: VegaLiteSpec[]) => {
        if (abortRef.current !== controller) return;
        finishStreaming(finalText, finalCharts);
        abortRef.current = null;
      };

      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({
            conversation_id: conversationId,
            message: text,
            filters: activeFilters.length > 0 ? filters : undefined,
          }),
        });

        if (!res.ok) {
          finishIfActive(`Error: ${res.statusText}`, []);
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
          // Normalize CRLF to LF so event splitting works across SSE servers.
          buffer = buffer.replace(/\r/g, '');
          lastEventAt = Date.now();
          // Process complete SSE events (separated by a blank line).
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
              const data = JSON.parse(dataStr);

              switch (eventType) {
                case 'text_delta':
                  collectedText += data.content || '';
                  appendStreamingText(data.content || '');
                  break;

                case 'tool_start':
                  setToolStatus({
                    tool: data.tool,
                    status: 'running',
                  });
                  break;

                case 'tool_result':
                  setToolStatus({ tool: data.tool, status: 'done' });
                  break;

                case 'status':
                  if (data.message) {
                    setToolStatus({ tool: String(data.message), status: 'running' });
                  }
                  break;

                case 'chart':
                  if (data.spec) {
                    collectedCharts.push(data.spec);
                    addStreamingChart(data.spec);
                  }
                  break;

                case 'conversation':
                  if (data.id && data.title) {
                    const activeIdNow = useChatStore.getState().activeConversationId;
                    if (!activeIdNow) {
                      setActiveConversation(data.id as string);
                      addConversation({
                        id: data.id as string,
                        title: data.title as string,
                        created_at: new Date().toISOString(),
                        updated_at: new Date().toISOString(),
                      });
                    } else {
                      updateConversationTitle(
                        data.id as string,
                        data.title as string
                      );
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
                        ? data.charts
                        : []
                  );
                  return;

                case 'error':
                  terminalEventSeen = true;
                  finishIfActive(
                    `Error: ${data.message || 'Unknown error'}`,
                    []
                  );
                  return;
              }
            } catch {
              // skip malformed event payloads
            }
            lastEventAt = Date.now();
          }

          if (Date.now() - lastEventAt > streamTimeoutMs) {
            finishIfActive('Error: Stream timed out waiting for response.', collectedCharts);
            return;
          }
        }

        // If stream ended without done/error, always terminate client streaming state.
        if (!terminalEventSeen) {
          if (collectedText || collectedCharts.length > 0) {
            finishIfActive(collectedText, collectedCharts);
          } else {
            finishIfActive(
              'Error: Stream ended before a final response was received.',
              []
            );
          }
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          if (abortRef.current === controller) {
            abortRef.current = null;
          }
          return;
        }
        finishIfActive(`Error: ${err instanceof Error ? err.message : 'Network error'}`, []);
      }
    },
    [
      activeFilters,
      startStreaming,
      appendStreamingText,
      addStreamingChart,
      setToolStatus,
      finishStreaming,
      addMessage,
      setActiveConversation,
      addConversation,
      updateConversationTitle,
    ]
  );

  return { sendMessage };
}
