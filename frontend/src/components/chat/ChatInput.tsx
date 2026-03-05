import { useState, useRef, useEffect } from 'react';
import { Send, Square } from 'lucide-react';
import { abortActiveChatStream } from '../../hooks/useChat';
import { useChatStore } from '../../stores/chat-store';
import { useFilterStore } from '../../stores/filter-store';

interface ChatInputProps {
  onSend: (message: string) => void;
}

export function ChatInput({ onSend }: ChatInputProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const activeFilters = useFilterStore((s) => s.activeFilters);

  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  const handleSubmit = () => {
    if (text.trim()) {
      onSend(text.trim());
      setText('');
      if (textareaRef.current) {
        textareaRef.current.style.height = 'auto';
      }
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 200) + 'px';
    }
  };

  return (
    <div className="border-t border-[var(--color-border)] p-4 bg-[var(--color-surface)]">
      {activeFilters.length > 0 && (
        <div className="max-w-3xl mx-auto mb-3 flex flex-wrap items-center gap-2">
          {activeFilters.map((filter) => (
            <span
              key={`${filter.demo_id}:${filter.demo_level}`}
              className="inline-flex items-center rounded-full bg-[var(--color-primary-light)] px-2.5 py-1 text-[11px] font-medium text-[var(--color-primary)]"
            >
              {filter.demo_level.replace(/^(TOTAL|CUSTOMER|PROSPECT): /, '')}
            </span>
          ))}
          <span className="text-[11px] text-[var(--color-text-secondary)]">
            New questions will use this segment by default.
          </span>
        </div>
      )}
      <div className="flex gap-2 items-end max-w-3xl mx-auto">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder="Ask about the survey data..."
          rows={1}
          className="flex-1 resize-none rounded-xl border border-[var(--color-border)] px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-[var(--color-primary)] focus:border-transparent disabled:opacity-50 bg-[var(--color-surface)]"
        />
        {isStreaming && (
          <button
            type="button"
            onClick={() => abortActiveChatStream()}
            className="inline-flex items-center gap-1 rounded-xl border border-[var(--color-border)] px-3 py-3 text-sm text-[var(--color-text-secondary)] transition-colors hover:bg-[var(--color-surface-alt)]"
          >
            <Square className="h-4 w-4" />
            <span className="hidden sm:inline">Stop</span>
          </button>
        )}
        <button
          onClick={handleSubmit}
          disabled={!text.trim()}
          className="p-3 rounded-xl bg-[var(--color-primary)] text-white disabled:opacity-40 hover:opacity-90 transition-opacity"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}
