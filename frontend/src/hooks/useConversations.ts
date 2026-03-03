import { useEffect, useCallback } from 'react';
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
      setActiveConversation(id);
      try {
        const res = await fetch(`/api/conversations/${id}/messages`);
        if (res.ok) {
          const msgs: Message[] = await res.json();
          setMessages(msgs);
        }
      } catch {
        // ignore
      }
    },
    [setActiveConversation, setMessages]
  );

  const newConversation = useCallback(() => {
    setActiveConversation(null);
    setMessages([]);
  }, [setActiveConversation, setMessages]);

  const deleteConversation = useCallback(
    async (id: string) => {
      try {
        await fetch(`/api/conversations/${id}`, { method: 'DELETE' });
        removeConversation(id);
      } catch {
        // ignore
      }
    },
    [removeConversation]
  );

  useEffect(() => {
    loadConversations();
  }, [loadConversations]);

  return { loadConversations, selectConversation, newConversation, deleteConversation };
}
