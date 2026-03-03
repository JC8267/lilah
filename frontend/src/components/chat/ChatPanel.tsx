import { useRef, useEffect } from 'react';
import { BarChart3 } from 'lucide-react';
import { useChatStore } from '../../stores/chat-store';
import { useChat } from '../../hooks/useChat';
import { MessageBubble } from './MessageBubble';
import { StreamingText } from './StreamingText';
import { ChatInput } from './ChatInput';

interface ChatPanelProps {
  className?: string;
}

export function ChatPanel({ className = '' }: ChatPanelProps) {
  const messages = useChatStore((s) => s.messages);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const { sendMessage } = useChat();
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  return (
    <div className={`flex flex-col h-full ${className}`}>
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center h-full text-center">
            <div className="w-16 h-16 rounded-full bg-[var(--color-primary-light)] flex items-center justify-center mb-4">
              <BarChart3 className="w-8 h-8 text-[var(--color-primary)]" />
            </div>
            <h2 className="text-xl font-semibold text-[var(--color-text)] mb-2">
              What would you like to explore?
            </h2>
            <p className="text-sm text-[var(--color-text-secondary)] max-w-md">
              I can help you analyze IKEA home furnishing survey data from 24K respondents —
              just ask a question or pick a suggestion below.
            </p>
            <div className="mt-6 flex flex-col gap-2 max-w-md w-full">
              {[
                'What types of homes do people live in?',
                'How does furniture ownership differ by income?',
                'What are the top planned purchases for kitchens?',
                'Show me demographic breakdowns by region',
              ].map((q) => (
                <button
                  key={q}
                  onClick={() => sendMessage(q)}
                  className="text-left text-sm px-3 py-2 rounded-lg border border-[var(--color-border)] hover:bg-[var(--color-surface-alt)] transition-colors text-[var(--color-text-secondary)]"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="max-w-3xl mx-auto space-y-4">
          {messages.map((msg) => (
            <MessageBubble key={msg.id} message={msg} />
          ))}
          {isStreaming && <StreamingText />}
          <div ref={bottomRef} />
        </div>
      </div>

      <ChatInput onSend={sendMessage} />
    </div>
  );
}
