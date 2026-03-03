import { Plus, Trash2 } from 'lucide-react';
import { useChatStore } from '../../stores/chat-store';
import { useConversations } from '../../hooks/useConversations';
import { FilterSidebar } from '../filters/FilterSidebar';

export function Sidebar() {
  const conversations = useChatStore((s) => s.conversations);
  const activeId = useChatStore((s) => s.activeConversationId);
  const conversationsLoading = useChatStore((s) => s.conversationsLoading);
  const { selectConversation, newConversation, deleteConversation } =
    useConversations();

  return (
    <aside className="w-64 border-r border-[var(--color-border)] bg-[var(--color-surface-alt)] flex flex-col h-full">
      <div className="p-3 border-b border-[var(--color-border)]">
        <button
          onClick={newConversation}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-[var(--color-primary)] text-white text-sm font-medium hover:bg-[#004a8a] transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Chat
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {conversationsLoading ? (
          <div className="space-y-2 p-2">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-8 rounded-lg bg-gray-200 animate-pulse" />
            ))}
          </div>
        ) : (
          <>
            {conversations.map((conv) => (
              <div
                key={conv.id}
                className={`group flex items-center gap-2 px-3 py-2 rounded-lg cursor-pointer text-sm transition-colors ${
                  activeId === conv.id
                    ? 'bg-[var(--color-primary-light)] text-[var(--color-primary)] font-medium'
                    : 'text-[var(--color-text-secondary)] hover:bg-gray-100'
                }`}
                onClick={() => selectConversation(conv.id)}
              >
                <span className="flex-1 truncate">{conv.title}</span>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    deleteConversation(conv.id);
                  }}
                  className="opacity-0 group-hover:opacity-100 p-1 hover:text-red-500 transition-opacity"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))}
            {conversations.length === 0 && (
              <p className="text-xs text-[var(--color-text-secondary)] text-center py-8">
                No conversations yet. Start a new chat!
              </p>
            )}
          </>
        )}
      </div>

      <FilterSidebar />
    </aside>
  );
}
