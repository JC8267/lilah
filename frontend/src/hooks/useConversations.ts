import { useEffect, useCallback, useRef } from 'react';
import { abortActiveChatStream } from './useChat';
import { useChatStore } from '../stores/chat-store';
import type { Message } from '../types';

export function useConversations() {
  const {
    setConversations,
    setConversationsLoading,
    setActiveConversation,
    setMessages,
    removeConversation,
  } = useChatStore();
  const isStreaming = useChatStore((state) => state.isStreaming);
  const selectionAbortRef = useRef<AbortController | null>(null);

  const loadConversations = useCallback(async () => {
    setConversationsLoading(true);
    try {
      const res = await fetch('/api/conversations');
      if (res.ok) {
        const data = await res.json();
        setConversations(data);
      }
    } catch {
      // ignore
    } finally {
      setConversationsLoading(false);
    }
  }, [setConversations, setConversationsLoading]);

  const selectConversation = useCallback(
    async (id: string) => {
      if (isStreaming) {
        abortActiveChatStream();
      }

      selectionAbortRef.current?.abort();
      selectionAbortRef.current = new AbortController();

      setActiveConversation(id);
      setMessages([]);

      try {
        const res = await fetch(`/api/conversations/${id}/messages`, {
          signal: selectionAbortRef.current.signal,
        });
        if (res.ok) {
          const msgs: Message[] = await res.json();
          setMessages(msgs);
        }
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') {
          return;
        }
      }
    },
    [isStreaming, setActiveConversation, setMessages]
  );

  const newConversation = useCallback(() => {
    if (isStreaming) {
      abortActiveChatStream();
    }
    selectionAbortRef.current?.abort();
    setActiveConversation(null);
    setMessages([]);
  }, [isStreaming, setActiveConversation, setMessages]);

  const deleteConversation = useCallback(
    async (id: string) => {
      if (isStreaming) {
        abortActiveChatStream();
      }
      try {
        await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
        removeConversation(id);
      } catch {
        // ignore
      }
    },
    [isStreaming, removeConversation]
  );

  useEffect(() => {
    loadConversations();
    return () => {
      selectionAbortRef.current?.abort();
    };
  }, [loadConversations]);

  return { loadConversations, selectConversation, newConversation, deleteConversation };
}
